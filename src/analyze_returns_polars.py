import os
import numpy as np
import polars as pl
import scipy.stats as stats


def load_binance_1s_bars_polars(file_path: str) -> pl.DataFrame:
    """
    Ingests unzipped historical 1s bar files from Binance Vision using Polars.
    Maps positional arrays to standard financial indices with explicit types.
    """
    columns = [
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_volume', 'taker_buy_quote_volume', 'ignore'
    ]
    
    # Read CSV lightning fast directly into Arrow memory
    df = pl.read_csv(
        file_path,
        has_header=False,
        new_columns=columns,
        dtypes={
            'open_time': pl.Int64, 'open': pl.Float64, 'high': pl.Float64,
            'low': pl.Float64, 'close': pl.Float64, 'volume': pl.Float64,
            'close_time': pl.Int64, 'quote_asset_volume': pl.Float64,
            'number_of_trades': pl.Int32, 'taker_buy_base_volume': pl.Float64,
            'taker_buy_quote_volume': pl.Float64, 'ignore': pl.Float64
        }
    )
    
    # Convert integer millisecond epoch to native Polars Datetime type
    return df.with_columns(
        pl.from_epoch("open_time", time_unit="ms")
    ).sort("open_time")


def calculate_hurst(price_array: np.ndarray) -> float:
    """
    Computes Hurst Exponent via log-log linear regression of Rescaled Range (R/S).
    Expects a 1D NumPy array of sequential prices for raw performance.
    """
    returns = np.diff(np.log(price_array))
    N = len(returns)
    if N < 100:
        return np.nan
    
    max_lag = int(np.floor(N / 2))
    lags = np.unique(np.floor(np.logspace(np.log10(10), np.log10(max_lag), 20)).astype(int))
    
    RS_values = []
    valid_lags = []
    
    for lag in lags:
        num_blocks = int(np.floor(N / lag))
        if num_blocks == 0:
            continue
        rs_block_values = []
        for i in range(num_blocks):
            block = returns[i * lag: (i + 1) * lag]
            mean_centered = block - np.mean(block)
            cum_dev = np.cumsum(mean_centered)
            R = np.max(cum_dev) - np.min(cum_dev)
            S = np.std(block)
            if S > 0:
                rs_block_values.append(R / S)
        if len(rs_block_values) > 0:
            RS_values.append(np.mean(rs_block_values))
            valid_lags.append(lag)
    
    if len(valid_lags) < 2:
        return np.nan
    
    poly = np.polyfit(np.log(valid_lags), np.log(RS_values), 1)
    return poly[0]


def analyze_scale_distributions(df: pl.DataFrame):
    # Map durations to Polars dynamic windowing interval strings
    durations = {
        '1 Minute': '1m',
        '5 Minutes': '5m',
        '15 Minutes': '15m',
        '1 Hour': '1h',
        '4 Hours': '4h',
        '1 Day': '1d'
    }
    
    print("\n=======================================================")
    print("        SCALING RETURN DISTRIBUTION METRICS (POLARS)")
    print("=======================================================\n")
    
    for label, interval in durations.items():
        # Leverage Polars dynamic group_by to aggregate intervals instantly
        resampled = (
            df.group_by_dynamic("open_time", every=interval)
            .agg(pl.col("close").last())
            .drop_nulls()
        )
        
        if resampled.height < 15:
            print(f"{label}: Insufficient data samples ({resampled.height}) to analyze.")
            continue
        
        # Extract underlying vector directly to NumPy memory without copying data
        close_prices = resampled["close"].to_numpy()
        log_returns = np.diff(np.log(close_prices))
        
        # Calculate skew, kurtosis, and normality
        skew = stats.skew(log_returns)
        kurt = stats.kurtosis(log_returns)  # Excess Kurtosis (Normal = 0)
        jb_stat, p_val = stats.jarque_bera(log_returns)
        
        print(f"--- {label} Horizon (N={len(log_returns)}) ---")
        print(f"  Skewness       : {skew:.4f}")
        print(f"  Excess Kurtosis: {kurt:.4f} (Values > 0 imply Heavy Tails)")
        print(f"  Jarque-Bera Stat: {jb_stat:.2f} (p-value: {p_val:.4e})")
        print(f"  Conforms to Normal Distribution: {'YES' if p_val > 0.05 else 'NO'}\n")


if __name__ == '__main__':
    # Usage Example:
    # file_target = 'data_capture/BTCUSDT/BTCUSDT-klines-2026-06-20.csv'
    # if os.path.exists(file_target):
    #     df_market = load_binance_1s_bars_polars(file_target)
    #     analyze_scale_distributions(df_market)
    #     
    #     # Global Hurst Calculation over whole raw vector
    #     hurst_val = calculate_hurst(df_market["close"].to_numpy())
    #     print(f"Global Base Asset Hurst Exponent: {hurst_val:.4f}")
    pass