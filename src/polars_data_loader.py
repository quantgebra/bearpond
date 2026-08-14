import os
import polars as pl
import numpy as np
import scipy.stats as stats
import seaborn as sns
import matplotlib.pyplot as plt


DATA_ROOT = os.path.abspath("../polymarket-data/data")


# 1. Initialize the directory scanner
q = (
    pl.scan_parquet(f"{DATA_ROOT}/**/*.parquet", hive_partitioning=True)
    .filter(
        (pl.col("source") == "binance") &
        (pl.col("market_type") == "spot") &
        (pl.col("data_type") == "klines") &
        (pl.col("bar_size") == "1s") &
        (pl.col("symbol") == "BTCUSDT")
    )
    
    # 2. Force uniqueness across all discovered UUID files in memory
    # 'maintain_order=True' ensures chronological consistency
    .unique(subset=["open_time"], keep="any", maintain_order=True)
)

# 3. Collect the clean, deduplicated matrix
df = q.collect()

df_sorted = df.sort("open_time")

# 1. Calculate the base continuous Log Price Vector
df_with_log = df_sorted.with_columns(
    pl.col("close").log().alias("log_close")
)

# 2. Extract multi-scale successive returns using Polars expressions
# We use .diff(n) which calculates: log_close[t] - log_close[t-n]
distribution_df = df_with_log.with_columns([
    pl.col("log_close").diff(1).alias("returns_1s"),
    pl.col("log_close").diff(10).alias("returns_10s"),
    pl.col("log_close").diff(60).alias("returns_1m"),
    pl.col("log_close").diff(300).alias("returns_5m"),
    pl.col("log_close").diff(600).alias("returns_10m"),
    pl.col("log_close").diff(3600).alias("returns_60m"),
]).drop_nulls()

# 3. Analyze the structural shapes of each distribution
horizons = ["returns_1s", "returns_10s", "returns_1m", "returns_5m", "returns_10m", "returns_60m"]

print("=====================================================================")
# Polars makes it easy to compute descriptive statistics across columns instantly
summary = distribution_df.select(
    [pl.col(h).skew().alias(f"{h}_skew") for h in horizons] +
    [(pl.col(h).kurtosis()).alias(f"{h}_excess_kurtosis") for h in horizons]
)

print(summary)
print("=====================================================================")

# 4. Filter out the zero-trade noise to see the true volatility variance
# If you want to look purely at times the price actually moved:
active_moves_1s = distribution_df.filter(pl.col("returns_1s") != 0)["returns_1s"].to_numpy()

print(f"Total 1s Bars: {distribution_df.height}")
print(f"Bars with zero price movement: {distribution_df.height - len(active_moves_1s)}")
print(f"True Active 1s Kurtosis (Ex-Zero Noise): {stats.kurtosis(active_moves_1s):.4f}")



horizons = ["1s", "10s", "1m", "5m", "10m", "60m"]

# 3. Initialize matplotlib grid layout (2x2 Multi-Panel Grid)
fig, axes = plt.subplots(2, 3, figsize=(14, 10), sharey=False)
axes = axes.flatten()

print("Generating distribution overlays...")
for i, horizon in enumerate(horizons):
    ax = axes[i]
    
    distribution_df = distribution_df.with_columns(
        pl.col(f"returns_{horizon}").abs().alias(f"returns_{horizon}_abs")
    )
    
    # Extract the specific clean vector to a zero-copy numpy array
    # We filter out exactly 0 to inspect structural price adjustments
    raw_data = distribution_df[f"returns_{horizon}_abs"].to_numpy()
    # active_returns = raw_data[raw_data != 0]
    active_returns = raw_data
    
    if len(active_returns) == 0:
        continue

    # Calculate localized kurtosis for chart notation
    excess_kurt = stats.kurtosis(active_returns)

    # Standardize the data (Z-score) so we can easily compare it to a Standard Normal Curve
    mean = np.mean(active_returns)
    std = np.std(active_returns)
    standardized_returns = (active_returns - mean) / std

    max_val = np.max(np.abs(active_returns))

    # Generate log-spaced bins for the positive side, and mirror them for the negative side
    pos_bins = np.logspace(np.log10(1e-8), np.log10(max_val), num=100)
    # neg_bins = -pos_bins[::-1]
    # custom_bins = np.concatenate([neg_bins, [0], pos_bins])
    custom_bins = np.concatenate([[0], pos_bins])

    # 3. Plot using your custom bin boundaries
    # ax.hist(active_returns, bins=custom_bins, color="royalblue", edgecolor="royalblue", alpha=0.7)
    ax.hist(
        active_returns,
        bins=custom_bins,
        color="royalblue",
        alpha=0.6,
        edgecolor="royalblue",
        lw=0.5,
        label="Empirical Count"
    )
    ax.set_yscale('log')

    # Dynamic styling and text adjustments
    ax.set_title(f"Horizon: {horizon} (Excess Kurtosis: {excess_kurt:.2f})", fontsize=12, fontweight='bold')
    ax.set_xlabel("Log Return Magnitude")
    ax.set_ylabel("Number of Occurrences (Log Count)")
    ax.grid(True, which="both", alpha=0.2)
    
    if i == 0:
        ax.legend()

plt.suptitle("Evolution of Log Return Distributions Across Time Horizons (BTCUSDT)", fontsize=16, fontweight='bold')
plt.tight_layout()

# Save chart directly to research output workspace
output_img_path = os.path.join(DATA_ROOT, "return_distributions_comparison.png")
plt.savefig(output_img_path, dpi=300)
print(f"📊 Matrix graph successfully rendered and saved to: {output_img_path}")
plt.show()

# =================

plt.figure(figsize=(8, 5))

data = distribution_df[f"returns_1s"]
# 3. Create the histogram
# 'bins' controls the granularity, 'edgecolor' visually separates the bars
plt.hist(data, bins=30, color="royalblue", edgecolor="black", alpha=0.7)

# 4. Add clear labels and context
plt.title("Standard Normal Distribution Histogram", fontsize=14, fontweight="bold")
plt.xlabel("Value Range")
plt.ylabel("Frequency (Count)")
plt.grid(True, linestyle="--", alpha=0.5) # Add a faint grid for scannability

# 5. Render and display the chart
plt.tight_layout()
plt.show()


# =================


# 1. Extract your raw returns vector
returns = distribution_df[f"returns_1s"].to_numpy()

# 2. Create high-resolution bins around zero, then exponentially expand to the edges
# We find the absolute max outlier to bound our edges
max_val = np.max(np.abs(returns))

# Generate log-spaced bins for the positive side, and mirror them for the negative side
pos_bins = np.logspace(np.log10(1e-6), np.log10(max_val), num=100)
neg_bins = -pos_bins[::-1]
custom_bins = np.concatenate([neg_bins, [0], pos_bins])

# 3. Plot using your custom bin boundaries
plt.figure(figsize=(10, 6))
plt.hist(returns, bins=custom_bins, color="royalblue", edgecolor="black", alpha=0.7)

# CRITICAL: You MUST use a log scale on the X-axis or Y-axis to see the detail!
plt.yscale('log') # This stops the central spike from completely swallowing the rest of the chart
plt.title("High-Resolution Log-Spaced Return Distribution", fontweight="bold")
plt.xlabel("Return Scale")
plt.ylabel("Frequency Count (Log Scale)")
plt.show()
