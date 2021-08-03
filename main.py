import pandas_ta as pta
import time
import sqlalchemy
import math
import config
from trader import TraderAPI
from wallet import Portfolio
from strategies import *

MA1 = 40
MA2 = 170
STD = 20
M30 = ("30m", 1800)
M15 = ("15m", 900)
M1 = ("1m", 60)
H4 = ("4h", 14400)
H1 = ("1h", 3600)
LOG_COLUMNS = ["Timestamp", "Asset", "Action", "Price", "Volume", "Strategy"]


# ----- CLEAN DATA FOR USAGE ----- #

def calculate_rsi(df_column, window=14):
    """Calculates RSI for price"""
    delta = df_column.diff(1)

    positives = delta.copy()
    negatives = delta.copy()
    positives[positives < 0] = 0
    negatives[negatives > 0] = 0

    rs = positives.rolling(window).mean() / abs(negatives.rolling(window).mean())
    rsi = 100 - 100 / (1 + rs)
    return rsi


def create_dataframe(symbol, interval, limit):
    """Cleans data for the bot to use"""

    # Create and clean initial DataFrame
    df = pd.DataFrame(api_trader.get_history(symbol=symbol, interval=interval, limit=limit))
    df = df.drop(columns=df.iloc[:, 2:].columns)
    df.columns = ["Open Time", "Price"]
    df = df.set_index("Open Time")
    df.index = pd.to_datetime(df.index, unit="ms")
    df = df.astype(float)

    # Calculate SMA
    df[f"SMA_{MA1}"] = df["Price"].rolling(window=MA1).mean()
    df[f"SMA_{MA2}"] = df["Price"].rolling(window=MA2).mean()

    # Calculate Bollinger bands
    df["Std"] = df["Price"].rolling(window=STD).std()
    df["Upper"] = df[f"SMA_{MA1}"] + 1.5 * df["Std"]
    df["Lower"] = df[f"SMA_{MA1}"] - 2.5 * df["Std"]

    # Calculate RSI with SMA
    df["RSI"] = pta.rsi(df["Price"], length=14)
    return df


def calc_order_quantity(tick, coins):
    """Formats coins float variable for orders"""
    return math.floor(coins * 10 ** tick) / float(10 ** tick)


def get_balance(trader, currency):
    """Get float value of spendable currency"""
    for balance in trader.get_balance()["balances"]:
        if balance["asset"] == currency:
            return float(balance["free"])


def get_step_size(trader, asset_symbol):
    """Get the step size for crypto currencies used by the api"""
    for symbol in trader.get_exchange_info(asset_symbol)["symbols"]:
        if symbol["symbol"] == asset:
            for binance_filter in symbol["filters"]:
                if binance_filter['filterType'] == 'LOT_SIZE':
                    return binance_filter['stepSize'].find('1') - 2


# ----- VISUAL FEEDBACK ----- #

def add_border(message):
    """Add a border to message"""
    formatted_message = "<-------------------------" + message
    while len(formatted_message) < 92:
        formatted_message += "-"
    return formatted_message + ">"


def show_data_message(df, asset_symbol, strategy_type):
    """Print new data result"""
    print(add_border(f"RETRIEVING DATA FOR {asset_symbol} {strategy_type} STRATEGY"))
    print(df.iloc[[-1]])
    print(add_border(""))


def show_order_message(order_action, active_asset):
    """Print when order is placed"""
    print(add_border(f"{order_action} ORDER PLACED FOR {active_asset}"))
    print(add_border(f"NEW BALANCE: {user_portfolio.total_balance}"))
    print(add_border(""))


# ----- LOGGING NEW ORDERS ----- #

def log_buy_order(asset_symbol, coins, strategy_name, ratio, db_engine):
    """Saves active orders to load when restarting."""
    row = {"asset": [asset_symbol], "coins": [coins], "ratio": [ratio], "strategy": [strategy_name]}
    df = pd.DataFrame(row)
    df.to_sql("active_trades", db_engine, if_exists="append", index=False)


def delete_buy_order(db_engine, asset_symbol, strategy_name):
    """Delete buy order from database when asset is sold."""
    metadata = sqlalchemy.MetaData()
    table = sqlalchemy.Table("active_trades", metadata, autoload_with=db_engine)
    action_to_execute = table.delete().where(table.columns.asset == asset_symbol,
                                             table.columns.strategy == strategy_name)
    with db_engine.connect() as connection:
        connection.execute(action_to_execute)


# ----- PREPARE BOT ----- #

# Create database connection and load active orders
engine = sqlalchemy.create_engine(f"sqlite:///{config.db_path}")
active_trades = pd.read_sql("active_trades", engine)
active_trades = active_trades.set_index("asset")

# Create all instances
api_trader = TraderAPI()
user_portfolio = Portfolio(get_balance(api_trader, "EUR"))
crossing_sma = CrossingSMA(MA1, MA2, interval=H4, name="GOLDEN CROSS", balance=0.50, db_engine=engine)
bottom_rsi = BottomRSI(interval=H1, name="RSI DIPS", balance=0.25, db_engine=engine)
bollinger = BollingerBands(interval=M15, name="BOL BANDS", balance=0.25, db_engine=engine)

# Prepare instances
strategies = (crossing_sma, bottom_rsi, bollinger)
user_portfolio.active_trades += float(active_trades["ratio"].sum())

# ----- BOT ----- #

just_posted = False
while True:
    current_time = time.time()

    for strategy in strategies:

        # Use UNIX to determine when to load interval data
        if -1 <= (current_time % strategy.interval[1]) <= 1:
            time.sleep(15)

            # Test strategy for every asset in Portfolio
            for asset in user_portfolio.assets:
                df_asset = create_dataframe(asset, strategy.interval[0], MA2)
                show_data_message(df_asset, asset, strategy.name)
                action = strategy.check_for_signal(df_asset, asset)

                # ----- BUY SIGNAL ----- #

                if action == "BUY":
                    trade_amount = user_portfolio.calc_available_balance(strategy.ratio / len(user_portfolio.assets))
                    receipt = api_trader.post_order(asset=asset, quantity=round(trade_amount, 2), action=action)

                    if receipt["status"] == "FILLED":
                        new_coins = float(receipt["executedQty"]) * 0.998
                        ratio_balance = (strategy.ratio / len(user_portfolio.assets))

                        # Log the buy order when filled
                        log_buy_order(asset_symbol=asset, coins=new_coins, strategy_name=strategy.name,
                                      ratio=ratio_balance, db_engine=engine)
                        strategy.active_assets = strategy.set_active_assets(engine)
                        user_portfolio.active_trades += ratio_balance

                        # Reload user_portfolio balance and show user
                        user_portfolio.total_balance = get_balance(api_trader, "EUR")
                        show_order_message(action, asset)

                # ----- SELL SIGNAL ----- #

                elif action == "SELL":

                    # Calculate correct amount of coins to sell
                    asset_step_size = get_step_size(api_trader, asset)
                    coins_for_sale = float(strategy.active_assets.loc[asset, "coins"])
                    quantity = calc_order_quantity(asset_step_size, coins_for_sale)

                    # Place order
                    receipt = api_trader.post_order(asset=asset, quantity=quantity, action=action)

                    if receipt["status"] == "FILLED":

                        # Log sell order
                        delete_buy_order(engine, asset, strategy.name)
                        strategy.active_assets = strategy.set_active_assets(engine)
                        user_portfolio.active_trades -= (strategy.ratio / len(user_portfolio.assets))

                        # Reload balance and show user
                        user_portfolio.total_balance = get_balance(api_trader, "EUR")
                        show_order_message(action, asset)

            just_posted = True

    if just_posted:
        time.sleep(60)
        just_posted = False
