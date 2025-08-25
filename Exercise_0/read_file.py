import pandas as pd
from pathlib import Path

DATA_PATH = Path(__file__).parents[2] / "code" / "data"


df = pd.read_csv(DATA_PATH / "norway_new_car_sales_by_make.csv", index_col=0, parse_dates=True)

print(df.head())
