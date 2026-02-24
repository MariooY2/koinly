# Ethereum Wallet Transaction Analyzer

Analyzes all transactions for an Ethereum wallet using the Etherscan API, exports them to CSV, and produces a discrepancy report comparing expected vs actual on-chain balances.

## Features

- Fetches normal ETH transactions, internal transactions, and ERC20 token transfers
- Exports all transactions to a CSV file with date, hash, direction, addresses, amount, asset, and gas fee
- Calculates expected balances by summing inflows minus outflows minus gas fees
- Fetches actual on-chain balances and compares against expected
- Skips NFT transactions (ERC721/ERC1155)
- Handles pagination and Etherscan rate limits automatically

## Setup

1. Get a free API key from [etherscan.io](https://etherscan.io/apis)
2. Copy your key into the `.env` file:
   ```
   ETHERSCAN_API_KEY=your_key_here
   ```

## Usage

```bash
python script.py
```

### Output

- **transactions.csv** — full transaction history with columns:
  - Date, Transaction Hash, Type (IN/OUT), From, To, Amount, Token/Asset, Gas Fee (in ETH)
- **Discrepancy report** — printed to the console, showing expected vs actual balance for ETH and each ERC20 token

## Configuration

Edit the constants at the top of `script.py` to change:

| Variable | Description |
|----------|-------------|
| `CSV_FILE` | Output CSV filename |

## Notes

- Uses only the Python standard library (no external dependencies)
- Free Etherscan API tier is rate-limited to ~5 requests/second; the script handles this automatically
- Discrepancies are expected for wallets that interact with DeFi protocols (staking, LP rewards, rebasing tokens, airdrops) since those may not appear as standard transfer events
