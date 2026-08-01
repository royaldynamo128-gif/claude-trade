# Strategies

Place Freqtrade strategy `.py` files here to use them with ClaudeTrade.

Each file must contain exactly one class inheriting from `IStrategy`.

## Currently Available

| File | Class | Source |
|---|---|---|
| `CTSampleStrategy.py` | `CTSampleStrategy` | Freqtrade built-in template |

## Adding Strategies

See [../STRATEGIES.md](../STRATEGIES.md) for the full guide.

Quick add:
```bash
# 1. Copy strategy file here
cp /path/to/MyStrategy.py strategies/

# 2. Test it
source venv/bin/activate
freqtrade test-strategy --strategy MyStrategy --userdir user_data --config config/config.demo.json

# 3. Run it
STRATEGY=MyStrategy ./scripts/start.sh
```

## Community Strategy Repository

https://github.com/freqtrade/freqtrade-strategies
