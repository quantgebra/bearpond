import os
import glob
from zipfile import ZipFile
import polars as pl
from pathlib import Path
import shutil

# 1. Map paths relative to your shared monorepo data root
# DATA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../polymarket-data"))
INBOX_ROOT = os.path.abspath("../polymarket-data/inbox")
DATA_ROOT = os.path.abspath("../polymarket-data/data")
# SYMBOL = "BTCUSDT"
# BAR_SIZE = "1s"
# TARGET_DIR = os.path.join(DATA_ROOT, "polymarket", "spot", f"klines-{BAR_SIZE}")
# TARGET_FILE = os.path.join(INBOX_ROOT, 'binance-spot-klines-1s-BTCUSDT-2026-05-31')


def convert_zip_to_parquet():
    # Gather all zipped historical 1s bar data blocks downloaded by TypeScript
    zip_files = glob.glob(os.path.join(INBOX_ROOT, "*.zip"))
    
    if not zip_files:
        print(f"No archived zip targets found in: {INBOX_ROOT}")
        # return
    
    print(f"Found {len(zip_files)} archives for conversion...")
    
    columns = [
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_volume', 'taker_buy_quote_volume', 'ignore'
    ]
    
    for zip_path in zip_files:
        try:
            with ZipFile(zip_path, 'r') as archive:
                # Find the name of the compressed CSV file tucked inside the zip
                csv_filename = [f for f in archive.namelist() if f.endswith('.csv')][0]
                
                # Stream read the binary payload straight from the archive structure
                with archive.open(csv_filename) as csv_file:
                    # Ingest the binary stream instantly into a multi-threaded DataFrame
                    df = pl.read_csv(
                        csv_file.read(),
                        has_header=False,
                        new_columns=columns,
                    )
            
            file_name_parts = Path(zip_path).name.split('-')
            source = file_name_parts[0]
            market_type = file_name_parts[1]
            data_type = file_name_parts[2]
            bar_size = file_name_parts[3]
            symbol = file_name_parts[4]
            
            # Optional: Map open_time to clean Datetime structures right before writing
            df = df.with_columns(
                pl.from_epoch(pl.col("open_time"), time_unit="us").alias("open_time"),
                # pl.from_epoch(pl.col("close_time"), time_unit="us").alias("close_time")
            )
            
            # Extracts chronological partition segments and writes the data frame
            # into a structured Hive layout under data/hive_warehouse/
            # 1. Break out the timestamp components into dedicated layout columns
            # We add an 'asset' column as well to allow multiple cross-market assets to live in the same root
            processed_df = df.with_columns([
                pl.lit(source).alias("source"),
                pl.lit(market_type).alias("market_type"),
                pl.lit(data_type).alias("data_type"),
                pl.lit(bar_size).alias("bar_size"),
                pl.lit(symbol).alias("symbol"),
                pl.col("open_time").dt.year().alias("year"),
                pl.col("open_time").dt.month().alias("month"),
                pl.col("open_time").dt.day().alias("day")
            ])
            
            # Construct the explicit path to the target leaf folder
            sample_row = processed_df.head(1)
            if sample_row.height == 0:
                raise ValueError('Empty DataFrame')
            
            target_leaf = os.path.join(
                DATA_ROOT,
                f"source={sample_row['source'][0]}",
                f"market_type={sample_row['market_type'][0]}",
                f"data_type={sample_row['data_type'][0]}",
                f"bar_size={sample_row['bar_size'][0]}",
                f"symbol={sample_row['symbol'][0]}",
                f"year={sample_row['year'][0]}",
                f"month={sample_row['month'][0]}",
                f"day={sample_row['day'][0]}"
            )
            
            # STORAGE PURGE GUARD: If the folder exists, wipe all previous UUID files inside it
            # before writing the new definitive copy.
            if os.path.exists(target_leaf):
                print(f"[Idempotency Guard] Wiping older duplicate files in: {target_leaf}")
                shutil.rmtree(target_leaf)
            
            # 2. Write to disk utilizing PyArrow's partitioning engine
            # This automatically segments the rows and generates folders dynamically
            processed_df.write_parquet(
                DATA_ROOT,
                use_pyarrow=True,
                pyarrow_options={
                    "partition_cols": ["source", "market_type", "data_type", "bar_size",
                                       "symbol", "year", "month", "day"],
                    "compression": "snappy"
                }
            )
            print(f"Successfully sync'd matrix blocks to Hive warehouse at: {DATA_ROOT}")
        
        
        except Exception as e:
            print(f" ❌ Conversion failure on block {os.path.basename(zip_path)}: {str(e)}")


if __name__ == "__main__":
    convert_zip_to_parquet()
