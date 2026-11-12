x = df.groupby('code')['close'].shift(-1)
y = df['close'].rolling(5, center=True).mean()
