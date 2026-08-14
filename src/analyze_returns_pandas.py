import numpy as np
import pandas as pd
import scipy.stats as stats
import matplotlib.pyplot as plt

def load_binance_1s_bars(file_path: str) -> pd.DataFrame:
    """
    Ingests unzipped historical 1s bar files from Binance Vision.
    Maps positional arrays to standard financial indices.
    """
    columns = [
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_volume', 'taker_buy_quote_volume', 'ignore'
    ]
    df = pd.read_csv(file_path, names=columns, header=None)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    return df

def calculate_hurst(price_series: pd.Series) -> float:
    """
    Computes Hurst Exponent via log-log linear regression of Rescaled Range (R/S).
    """
    prices = price_series.values
    returns = np.diff(np.log(prices))
    N = len(returns)
    max_lag = int(np.floor(N / 2))
    lags = np.unique(np.floor(np.logspace(np.log10(10), np.log10(max_lag), 20)).astype(int))
    
    RS_values = []
    valid_lags = []
    
    for lag in lags:
        num_blocks = int(np.floor(N / lag))
        if num_blocks == 0: continue
        rs_block_values = []
        for i in range(num_blocks):
            block = returns[i*lag : (i+1)*lag]
            mean_centered = block - np.mean(block)
            cum_dev = np.cumsum(mean_centered)
            R = np.max(cum_dev) - np.min(cum_dev)
            S = np.std(block)
            if S > 0:
                rs_block_values.append(R / S)
        if len(rs_block_values) > 0:
            RS_values.append(np.mean(rs_block_values))
            valid_lags.append(lag)
            
    poly = np.polyfit(np.log(valid_lags), np.log(RS_values), 1)
    return poly[0]

def analyze_scale_distributions(df: pd.DataFrame):
    durations = {
        '1 Minute': '1min',
        '5 Minutes': '5min',
        '15 Minutes': '15min',
        '1 Hour': '1h',
        '4 Hours': '4h',
        '1 Day': '1D'
    }
    
    print("\n=======================================================")
    print("        SCALING RETURN DISTRIBUTION METRICS")
    print("=======================================================\n")
    
    for label, rule in durations.items():
        resampled = df['close'].resample(rule).last().dropna()
        if len(resampled) < 15:
            print(f"{label}: Insufficient data samples ({len(resampled)}) to analyze.")
            continue
            
        log_returns = np.diff(np.log(resampled.values))
        
        skew = stats.skew(log_returns)
        kurt = stats.kurtosis(log_returns) # Excess Kurtosis
        jb_stat, p_val = stats.jarque_bera(log_returns)
        
        print(f"--- {label} Horizon (N={len(log_returns)}) ---")
        print(f"  Skewness       : {skew:.4f}")
        print(f"  Excess Kurtosis: {kurt:.4f} (Values > 0 imply Heavy Tails)")
        print(f"  Jarque-Bera Stat: {jb_stat:.2f} (p-value: {p_val:.4e})")
        print(f"  Conforms to Normal Distribution: {'YES' if p_val > 0.05 else 'NO'}\n")

if __name__ == '__main__':
    # Usage: Point this path to your actual historical data directory
    df_market = load_binance_1s_bars('data_capture/BTCUSDT/BTCUSDT-klines-2026-06-20.csv')
    
    # For initial testing, using the simulated historical engine
    # pass
