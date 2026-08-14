import os
# import polars as pl
# import numpy as np
# import seaborn as sns
# import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import scipy.stats as stats
from scipy.optimize import curve_fit
import matplotlib.pyplot as plt
from scipy.special import gammainc
from scipy.stats import t


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


# Define the log-transformed Gamma functional form for regression
def log_gamma_func(x, beta0, alpha, beta1):
    # Enforce safe bounds to avoid log(0) or negative alpha/beta shape errors
    alpha = max(alpha, 1.0)
    beta1 = max(beta1, 1e-5)
    return beta0 + (alpha - 1) * np.log(x) - beta1 * x


horizons = ["1s", "10s", "1m", "5m", "10m", "60m"]

for horizon in horizons:
    distribution_df = distribution_df.with_columns(
        pl.col(f"returns_{horizon}").abs().alias(f"returns_{horizon}_abs")
    )

    raw_data = distribution_df[f"returns_{horizon}_abs"].to_numpy()
    active_returns = raw_data[raw_data != 0]
    
    # Generate empirical coordinates matching your layout
    counts, bin_edges = np.histogram(active_returns, bins=1000)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    
    # Isolate just the positive return tail where data exists
    pos_mask = (bin_centers > 0) & (counts > 0)
    X_pos = bin_centers[pos_mask]
    y_log = np.log(counts[pos_mask])
    
    # Run the non-linear regression fit
    # p0 provides initial guessing coordinates [beta0, alpha, beta1]
    popt, _ = curve_fit(log_gamma_func, X_pos, y_log, p0=[y_log.max(), 1.1, 100.0])
    
    print(f"[{horizon} Positive Tail Shape Fit]")
    print(f"  Intercept (beta0): {popt[0]:.2f}")
    print(f"  Dome Convexity (alpha): {popt[1]:.4f}")  # Near 1 = sharp, >1 = rounded dome
    print(f"  Tail Decay Rate (beta1): {popt[2]:.2f}\n")

# ======================================================================================================================

# 1. Initialize a 3x2 grid layout to match your 6-panel profile
fig, axes = plt.subplots(3, 2, figsize=(14, 16), sharex=False)
axes = axes.flatten()
for i, horizon in enumerate(horizons):
    ax = axes[i]
    
    distribution_df = distribution_df.with_columns(
        pl.col(f"returns_{horizon}").abs().alias(f"returns_{horizon}_abs")
    )

    raw_data = distribution_df[f"returns_{horizon}_abs"].to_numpy()
    # active_returns = raw_data[raw_data != 0]
    active_returns = raw_data
    
    # 2. Extract empirical histogram counts and bin centers
    counts, bin_edges = np.histogram(active_returns, bins=1000)
    # counts = counts / counts.sum()
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    
    mask_0 = np.isfinite(counts) & (counts > 0)
    counts = counts[mask_0]
    bin_centers = bin_centers[mask_0]
    log_log_counts = np.log(np.log(counts))
    
    # 3. Extract the clean arrays for the fit
    valid_mask = np.isfinite(log_log_counts) & (log_log_counts > 0)
    x_fit = bin_centers[valid_mask]
    y_fit = log_log_counts[valid_mask]
    
    ax.scatter(x_fit, y_fit, s=10, color="blue", zorder=3, label=f"n={len(y_fit)}")

    # 3. Run the linear regression (1 degree = linear)
    slope, intercept = np.polyfit(x_fit, y_fit, 1)
    print(f'Slope: {slope} intercept: {intercept}')
    
    # 4. Generate the regression line for plotting
    line_x = np.linspace(x_fit.min(), x_fit.max(), 100)
    line_y = slope * line_x + intercept
    
    # 5. Plot it back onto your axis
    ax.plot(line_x, line_y, color="red", linestyle="-", label=f"Fit (b={intercept:.2f} m={slope:.2f})")
    
    # 5. Apply layout boundaries and styles
    # ax.set_yscale('log')
    ax.set_ylim(bottom=1)  # Hard floor at 1 instance to remove fractional counts
    
    excess_kurt = stats.kurtosis(active_returns)
    ax.set_title(f"Horizon: {horizon} (Excess Kurtosis: {excess_kurt:.2f})", fontsize=12, fontweight='bold')
    ax.set_xlabel("Log Return Value")
    ax.set_ylabel("Number of Occurrences")
    ax.grid(True, which="both", alpha=0.15)
    
    ax.legend()

plt.suptitle("Empirical Distributions with Fitted Twin Log-Gamma Curves Overlaid",
             fontsize=16, fontweight='bold', y=0.98)
plt.tight_layout()
plt.show()


# ======================================================================================================================
fig, axes = plt.subplots(3, 2, figsize=(14, 16), sharex=False)
axes = axes.flatten()
for i, horizon in enumerate(horizons):
    ax = axes[i]

    fig, ax = plt.subplots(figsize=(8, 5))
    horizon = '10s'
    
    distribution_df = distribution_df.with_columns(
        pl.col(f"returns_{horizon}").abs().alias(f"returns_{horizon}_abs")
    )
    
    positive_returns = distribution_df[f"returns_{horizon}_abs"].to_numpy()
    sorted_returns = np.sort(positive_returns)
    N = len(sorted_returns)

    # Create the negative side by multiplying by -1 and reversing the order
    # This goes from most negative up to closest to zero
    # neg_sorted = -pos_sorted[::-1]
    
    # Combine them into a single perfectly symmetric array
    # symmetric_returns = np.concatenate([neg_sorted, pos_sorted])
    # symmetric_returns_sorted = np.sort(symmetric_returns)
    # N = len(symmetric_returns_sorted)
    
    # 2. Compute the empirical CDF probabilities: P(X <= x)
    # This generates a clean sequence from 1/N up to 1.0
    cdf_probabilities = np.arange(1, N + 1) / N
    
    # 4. Plot the CDF as a fine line or small scatter points
    # Using a line (plot) is generally cleaner for dense cumulative data
    ax.plot(
        sorted_returns,
        cdf_probabilities,
        color="blue",
        label="Empirical CDF",
        zorder=3,
        # s=10
    )
    
    x_pos = sorted_returns
    y_pos = cdf_probabilities
    
    # --------------------------

    # 3. Define the positive Log-Gamma CDF function
    # alpha: shape, beta: tail decay, lmbda: scale/location parameter
    def log_gamma_pos_cdf(x, alpha, beta, lmbda):
        # Protect against log of numbers <= 0 or negative arguments
        inner = 1.0 + x / np.maximum(lmbda, 1e-8)
        z = beta * np.log(inner)
        return gammainc(alpha, np.maximum(z, 0))
    
    
    # 4. Fit the parameters using curve_fit
    # Initial guesses: alpha=2.0, beta=1.5, lmbda=0.001
    initial_guess = [2.0, 1.5, 0.001]
    
    # Set bounds to keep parameters strictly positive
    param_bounds = ((1e-5, 1e-5, 1e-6), (np.inf, np.inf, np.inf))
    
    popt, pcov = curve_fit(
        log_gamma_pos_cdf, x_pos, y_pos, p0=initial_guess, bounds=param_bounds
    )
    fit_alpha, fit_beta, fit_lmbda = popt
    
    print("Fitted Log-Gamma Parameters:")
    print(f"  Alpha (α): {fit_alpha:.4f}")
    print(f"  Beta  (β): {fit_beta:.4f}")
    print(f"  Lambda(λ): {fit_lmbda:.6f}")
    
    # 5. Generate the model curve and project it back to the original CDF scale [0.5, 1.0]
    smooth_x = np.linspace(x_pos.min(), x_pos.max(), 1000)
    fitted_y_rescaled = log_gamma_pos_cdf(smooth_x, fit_alpha, fit_beta, fit_lmbda)
    fitted_y_original = 0.5 + (fitted_y_rescaled / 2)
    
    # 6. Plot the positive half fit onto your axis
    ax.plot(
        smooth_x,
        fitted_y_original,
        color="red",
        linestyle="-",
        linewidth=2,
        label="Log-Gamma Positive Fit",
    )
    
    # --------------------------
    
    # 2. Define the positive half of a centered Student's t CDF
    def student_t_pos_cdf(x, df, scale):
        # t.cdf(0) is always 0.5, so we shift and rescale
        raw_cdf = t.cdf(x, df=df, loc=0, scale=scale)
        return (raw_cdf - 0.5) * 2
    
    
    # 3. Fit (df ~ 2.5 for fat tails, scale ~ 0.0005 for a sharp wall)
    popt_t, _ = curve_fit(
        student_t_pos_cdf, x_pos, y_pos, p0=[2.5, 0.0005], bounds=((1.001, 1e-6), (20, 1))
    )
    
    # 4. Generate the model curve back on the [0.5, 1.0] scale
    smooth_x = np.linspace(x_pos.min(), x_pos.max(), 2000)
    y_fit_t = 0.5 + (student_t_pos_cdf(smooth_x, *popt_t) / 2)
    
    ax.plot(
        smooth_x, y_fit_t, color="green", linewidth=2, label=f"Student-t Fit (df={popt_t[0]:.2f})"
    )
    
    # --------------------------
    
    # Add reference lines for the center of the distribution
    ax.axhline(0.5, color="gray", linestyle="--", alpha=0.5)
    ax.axvline(0.0, color="gray", linestyle="--", alpha=0.5)
    ax.grid(True, linestyle=":", alpha=0.6)
    # ax.set_yscale('log')

    # 1. Define the forward transformation and its inverse
    # Let's say we want a base-e exponential spacing: e^y
    forward_func = lambda y: np.exp(np.exp(y))
    inverse_func = lambda y: np.log(np.log(y))
    # 2. Apply it to the axis using FuncScale
    ax.set_yscale("function", functions=(forward_func, inverse_func))
    
    # excess_kurt = stats.kurtosis(active_returns)
    ax.set_title("Symmetric Empirical Cumulative Distribution Function (CDF)")
    ax.set_xlabel("Log Return Value")
    ax.set_ylabel("Cumulative Probability")
    ax.legend(loc="lower right")
    plt.show()

plt.suptitle("Empirical Distributions", fontsize=16, fontweight='bold', y=0.98)
plt.tight_layout()
plt.show()


    
