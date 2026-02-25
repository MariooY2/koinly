# Ethereum Wallet Transaction Analyzer

Analyzes all transactions for an Ethereum wallet using the Etherscan API, exports them to CSV and styled Excel files, and produces a discrepancy report comparing expected vs actual on-chain balances with automatic resolution.

## Features

- Fetches normal ETH transactions, internal transactions, and ERC20 token transfers
- Skips NFT transactions (ERC721/ERC1155)
- Exports all transactions to CSV and styled Excel (.xlsx) with colored rows by direction
- Calculates expected balances (inflows - outflows - gas) vs actual on-chain balances
- Queries historical on-chain balances via Alchemy archive node (`balanceOf()` at specific blocks)
- Auto-resolves discrepancies by detecting:
  - Rebasing/staking reward tokens (e.g. sOHM)
  - Fee-on-transfer / tax tokens (e.g. WAGMIGAMES)
  - Reflection/redistribution gains (e.g. DETF)
  - Scam/airdrop tokens with admin burns
- Handles pagination and Etherscan rate limits automatically

## Setup

1. Get a free API key from [etherscan.io](https://etherscan.io/apis)
2. Get an Alchemy API key from [alchemy.com](https://www.alchemy.com/)
3. Create a `.env` file:
   ```
   ETHERSCAN_API_KEY=your_etherscan_key
   WALLET_ADDRESS=0xYourWalletAddress
   ALCHEMY_API_KEY=your_alchemy_key
   ```

## Dependencies

```bash
pip install openpyxl
```

## Usage

```bash
python script.py
```

## Output

```
transactions/
  transactions.csv
  transactions.xlsx
discrepancies/
  discrepancies.csv
  discrepancies.xlsx
```

- **transactions.csv / .xlsx** — full transaction history with columns:
  - Date, Block Number, Transaction Hash, Type (IN/OUT), From, To, Amount, Token/Asset, Gas Fee (in ETH), Function, Input Data

- **discrepancies.csv / .xlsx** — per-token audit trail with columns:
  - Token/Asset, Contract Address, Row Type, Date, Block Number, Transaction Hash, Direction, From, To, Amount, Running Balance, On-Chain Balance, Function Called, Expected Balance, Actual Balance, Difference, Resolution

- **Console output** — summary of expected vs actual balances and discrepancy resolutions

## Configuration

Edit the constants at the top of `script.py` to change:

| Variable | Description |
|----------|-------------|
| `TRANSACTIONS_DIR` | Output folder for transaction files |
| `DISCREPANCIES_DIR` | Output folder for discrepancy files |
| `CHAIN_ID` | Blockchain network (default: `1` for Ethereum mainnet) |
| `PAGE_SIZE` | Max rows per Etherscan API page |

## Notes

- Requires `openpyxl` for Excel export; all other imports are from the Python standard library
- Free Etherscan API tier is rate-limited to ~5 requests/second; the script handles this automatically
- Alchemy archive node is used for historical `balanceOf()` queries to show on-chain balance at each transaction block
- Discrepancies are expected for wallets that interact with DeFi protocols (staking, LP rewards, rebasing tokens, airdrops) — the script auto-resolves these where possible
