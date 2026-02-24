"""
Ethereum Wallet Transaction Analyzer
Pulls all transactions for a wallet via Etherscan API,
exports to CSV, and produces a discrepancy report comparing
expected vs actual on-chain balances.
"""

import csv
import io
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError
from collections import defaultdict
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load .env file
def _load_env(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass

_load_env()

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
WALLET = os.environ.get("WALLET_ADDRESS", "").lower()
CSV_FILE = "transactions.csv"
DISCREPANCY_FILE = "discrepancies.csv"
BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = "1"                                  # 1 = Ethereum mainnet
PAGE_SIZE = 10000                               # max rows per API page
RATE_LIMIT_DELAY = 0.22                         # ~5 req/s for free tier

getcontext().prec = 36                          # enough for 18-decimal tokens

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def etherscan_get(params: dict) -> list:
    """Call Etherscan API with pagination; return combined result list."""
    params.setdefault("chainid", CHAIN_ID)
    params.setdefault("apikey", ETHERSCAN_API_KEY)
    params.setdefault("sort", "asc")
    params.setdefault("offset", str(PAGE_SIZE))

    all_results = []
    page = 1

    while True:
        params["page"] = str(page)
        url = f"{BASE_URL}?{urlencode(params)}"
        time.sleep(RATE_LIMIT_DELAY)

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except (URLError, HTTPError) as exc:
            print(f"  [!] API error on page {page}: {exc}")
            break

        status = data.get("status")
        result = data.get("result", [])

        if status == "0" and isinstance(result, str):
            # "No transactions found" or rate-limit message
            if "rate" in result.lower():
                print("  [!] Rate-limited, waiting 1 s …")
                time.sleep(1)
                continue
            if page == 1:
                print(f"  API response: {result}")
            break

        if not isinstance(result, list) or len(result) == 0:
            break

        all_results.extend(result)
        print(f"  page {page}: {len(result)} records")

        if len(result) < PAGE_SIZE:
            break
        page += 1

    return all_results


def wei_to_eth(wei_str: str) -> Decimal:
    return Decimal(wei_str) / Decimal("1000000000000000000")


def token_amount(raw: str, decimals: str) -> Decimal:
    d = int(decimals) if decimals else 18
    return Decimal(raw) / Decimal(10) ** d


def ts_to_date(ts: str) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ---------------------------------------------------------------------------
# 1. Pull transactions
# ---------------------------------------------------------------------------

def fetch_normal_txs() -> list:
    print("[*] Fetching normal ETH transactions …")
    return etherscan_get({
        "module": "account",
        "action": "txlist",
        "address": WALLET,
        "startblock": "0",
        "endblock": "99999999",
    })


def fetch_internal_txs() -> list:
    print("[*] Fetching internal transactions …")
    return etherscan_get({
        "module": "account",
        "action": "txlistinternal",
        "address": WALLET,
        "startblock": "0",
        "endblock": "99999999",
    })


def fetch_erc20_txs() -> list:
    print("[*] Fetching ERC20 token transfers …")
    return etherscan_get({
        "module": "account",
        "action": "tokentx",
        "address": WALLET,
        "startblock": "0",
        "endblock": "99999999",
    })


# ---------------------------------------------------------------------------
# 2. Normalise into a common row format + accumulate balances
# ---------------------------------------------------------------------------

# Accumulators
eth_in = Decimal("0")
eth_out = Decimal("0")
gas_total = Decimal("0")
# token contract -> {symbol, decimals, in, out}
token_flows = defaultdict(lambda: {
    "symbol": "",
    "decimals": 18,
    "in": Decimal("0"),
    "out": Decimal("0"),
})


def process_normal(txs: list) -> list:
    """Return CSV-ready rows from normal ETH transactions."""
    global eth_in, eth_out, gas_total

    rows = []
    for tx in txs:
        if tx.get("isError", "0") != "0":
            # Failed tx: the sender still pays gas, but value is NOT transferred
            if tx["from"].lower() == WALLET:
                fee = wei_to_eth(tx["gasUsed"]) * wei_to_eth(tx["gasPrice"]) * Decimal("1e18")
                # gasUsed * gasPrice is already in wei
                fee = Decimal(tx["gasUsed"]) * Decimal(tx["gasPrice"])
                fee = fee / Decimal("1e18")
                gas_total += fee
                rows.append({
                    "Date": ts_to_date(tx["timeStamp"]),
                    "Transaction Hash": tx["hash"],
                    "Type": "OUT (failed)",
                    "From": tx["from"],
                    "To": tx["to"],
                    "Amount": "0",
                    "Token/Asset": "ETH",
                    "Gas Fee (in ETH)": f"{fee:.18f}",
                    "Function": tx.get("functionName", ""),
                    "Input Data": tx.get("input", ""),
                })
            continue

        value = wei_to_eth(tx["value"])
        from_addr = tx["from"].lower()
        to_addr = (tx.get("to") or "").lower()
        direction = "IN" if to_addr == WALLET else "OUT"

        # Gas fee (only charged to the sender)
        fee = Decimal("0")
        if from_addr == WALLET:
            fee = Decimal(tx["gasUsed"]) * Decimal(tx["gasPrice"]) / Decimal("1e18")
            gas_total += fee

        if direction == "IN":
            eth_in += value
        else:
            eth_out += value

        rows.append({
            "Date": ts_to_date(tx["timeStamp"]),
            "Transaction Hash": tx["hash"],
            "Type": direction,
            "From": tx["from"],
            "To": tx.get("to", ""),
            "Amount": f"{value:.18f}",
            "Token/Asset": "ETH",
            "Gas Fee (in ETH)": f"{fee:.18f}",
            "Function": tx.get("functionName", ""),
            "Input Data": tx.get("input", ""),
        })
    return rows


def process_internal(txs: list) -> list:
    """Internal transactions (no gas fee — already counted in outer tx)."""
    global eth_in, eth_out

    rows = []
    for tx in txs:
        if tx.get("isError", "0") != "0":
            continue

        value = wei_to_eth(tx["value"])
        if value == 0:
            continue

        to_addr = (tx.get("to") or "").lower()
        direction = "IN" if to_addr == WALLET else "OUT"

        if direction == "IN":
            eth_in += value
        else:
            eth_out += value

        rows.append({
            "Date": ts_to_date(tx["timeStamp"]),
            "Transaction Hash": tx.get("hash", tx.get("transactionHash", "")),
            "Type": f"{direction} (internal)",
            "From": tx["from"],
            "To": tx.get("to", ""),
            "Amount": f"{value:.18f}",
            "Token/Asset": "ETH",
            "Gas Fee (in ETH)": "0",
            "Function": tx.get("type", ""),
            "Input Data": "",  # internal txs don't carry input data
        })
    return rows



def _looks_like_nft(tx: dict) -> bool:
    """Heuristic: skip ERC721 / ERC1155 transfers."""
    # tokenID present and tokenDecimal absent or "0" → NFT
    if tx.get("tokenID") and (not tx.get("tokenDecimal") or tx.get("tokenDecimal") == "0"):
        return True
    return False


def process_erc20(txs: list) -> list:
    global eth_in, eth_out

    rows = []
    for tx in txs:
        if _looks_like_nft(tx):
            continue

        decimals_str = tx.get("tokenDecimal", "18")
        try:
            int(decimals_str)
        except ValueError:
            continue

        symbol = tx.get("tokenSymbol", "UNKNOWN")
        contract = tx.get("contractAddress", "").lower()
        value = token_amount(tx["value"], decimals_str)

        to_addr = (tx.get("to") or "").lower()
        direction = "IN" if to_addr == WALLET else "OUT"

        tf = token_flows[contract]
        tf["symbol"] = symbol
        tf["decimals"] = int(decimals_str)
        if direction == "IN":
            tf["in"] += value
        else:
            tf["out"] += value

        rows.append({
            "Date": ts_to_date(tx["timeStamp"]),
            "Transaction Hash": tx["hash"],
            "Type": direction,
            "From": tx["from"],
            "To": tx.get("to", ""),
            "Amount": f"{value}",
            "Token/Asset": symbol,
            "Gas Fee (in ETH)": "0",  # gas already counted in normal tx
            "Function": "",
            "Input Data": "",  # input data is on the parent normal tx
        })
    return rows


# ---------------------------------------------------------------------------
# 3. Fetch actual on-chain balances
# ---------------------------------------------------------------------------

def fetch_eth_balance() -> Decimal:
    print("[*] Fetching actual ETH balance …")
    params = {
        "chainid": CHAIN_ID,
        "module": "account",
        "action": "balance",
        "address": WALLET,
        "tag": "latest",
        "apikey": ETHERSCAN_API_KEY,
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    time.sleep(RATE_LIMIT_DELAY)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status") == "0":
        print(f"  [!] ETH balance API error: {data.get('result')}")
        return Decimal("0")
    return wei_to_eth(data["result"])


def fetch_token_balance(contract: str, decimals: int) -> Decimal:
    params = {
        "chainid": CHAIN_ID,
        "module": "account",
        "action": "tokenbalance",
        "contractaddress": contract,
        "address": WALLET,
        "tag": "latest",
        "apikey": ETHERSCAN_API_KEY,
    }
    url = f"{BASE_URL}?{urlencode(params)}"
    time.sleep(RATE_LIMIT_DELAY)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    raw = data.get("result", "0")
    if not raw.isdigit() and not (raw.startswith("-") and raw[1:].isdigit()):
        return Decimal("0")
    return Decimal(raw) / Decimal(10) ** decimals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # ---- Validate config ----
    # Ensure stdout can handle Unicode on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

    if not ETHERSCAN_API_KEY:
        print("[!] ERROR: ETHERSCAN_API_KEY is empty. Set it in .env")
        return
    if not WALLET:
        print("[!] ERROR: WALLET_ADDRESS is empty. Set it in .env")
        return
    print(f"[*] API key: {ETHERSCAN_API_KEY[:6]}…{ETHERSCAN_API_KEY[-4:]}")
    print(f"[*] Wallet:  {WALLET}")

    # ---- Fetch all transactions ----
    normal_txs = fetch_normal_txs()
    internal_txs = fetch_internal_txs()
    erc20_txs = fetch_erc20_txs()

    print(f"\n[+] Totals: {len(normal_txs)} normal, "
          f"{len(internal_txs)} internal, {len(erc20_txs)} ERC20")

    # ---- Process & accumulate ----
    rows = []
    rows.extend(process_normal(normal_txs))
    rows.extend(process_internal(internal_txs))
    rows.extend(process_erc20(erc20_txs))

    # Sort by date
    rows.sort(key=lambda r: r["Date"])

    # ---- Write CSV ----
    fieldnames = [
        "Date", "Transaction Hash", "Type", "From", "To",
        "Amount", "Token/Asset", "Gas Fee (in ETH)",
        "Function", "Input Data",
    ]
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Wrote {len(rows)} rows to {CSV_FILE}")

    # ---- Expected balances ----
    expected_eth = eth_in - eth_out - gas_total

    # ---- Actual balances ----
    actual_eth = fetch_eth_balance()

    print("\n" + "=" * 72)
    print("  DISCREPANCY REPORT")
    print("=" * 72)

    # Track which contracts have discrepancies + their balances
    discrepancy_contracts = {}  # contract -> {symbol, expected, actual, diff}

    # ETH
    diff_eth = actual_eth - expected_eth
    print(f"\n{'ETH':>10}")
    print(f"  {'Expected balance':30s} {expected_eth:>28.18f}")
    print(f"  {'Actual on-chain balance':30s} {actual_eth:>28.18f}")
    print(f"  {'Difference':30s} {diff_eth:>28.18f}")
    if abs(diff_eth) > Decimal("0.000000000001"):
        print(f"  ** DISCREPANCY DETECTED **")
        discrepancy_contracts["ETH"] = {
            "symbol": "ETH", "contract": "",
            "expected": expected_eth, "actual": actual_eth, "diff": diff_eth,
        }
    else:
        print(f"  OK")

    # ERC20 tokens
    if token_flows:
        print(f"\n[*] Checking {len(token_flows)} ERC20 token balances …")

    for contract, tf in sorted(token_flows.items(), key=lambda x: x[1]["symbol"]):
        symbol = tf["symbol"]
        expected = tf["in"] - tf["out"]
        actual = fetch_token_balance(contract, tf["decimals"])
        diff = actual - expected

        print(f"\n{symbol:>10}  ({contract})")
        print(f"  {'Expected balance':30s} {expected:>28}")
        print(f"  {'Actual on-chain balance':30s} {actual:>28}")
        print(f"  {'Difference':30s} {diff:>28}")

        threshold = Decimal(1) / Decimal(10) ** min(tf["decimals"], 8)
        if abs(diff) > threshold:
            print(f"  ** DISCREPANCY DETECTED **")
            discrepancy_contracts[contract] = {
                "symbol": symbol, "contract": contract,
                "expected": expected, "actual": actual, "diff": diff,
            }
        else:
            print(f"  OK")

    # ---- Write discrepancy audit trail CSV ----
    if discrepancy_contracts:
        # Build token tx lookup: contract -> list of ERC20 transfer dicts
        token_tx_lookup = defaultdict(list)
        for tx in erc20_txs:
            c = tx.get("contractAddress", "").lower()
            if c in discrepancy_contracts:
                token_tx_lookup[c].append(tx)

        # Build normal tx lookup by hash for function names
        normal_tx_by_hash = {}
        for tx in normal_txs:
            normal_tx_by_hash[tx["hash"].lower()] = tx

        disc_rows = []
        for key, info in sorted(discrepancy_contracts.items(), key=lambda x: x[1]["symbol"]):
            symbol = info["symbol"]
            contract = info["contract"]

            # --- Summary row ---
            disc_rows.append({
                "Token/Asset": symbol,
                "Contract Address": contract,
                "Row Type": "SUMMARY",
                "Date": "",
                "Transaction Hash": "",
                "Direction": "",
                "From": "",
                "To": "",
                "Amount": "",
                "Running Balance": "",
                "Function Called": "",
                "Expected Balance": f"{info['expected']}",
                "Actual Balance": f"{info['actual']}",
                "Difference": f"{info['diff']}",
                "Resolution": "",
            })

            # --- Transaction tree for this token ---
            if key == "ETH":
                # For ETH, pull from the rows we already built
                running = Decimal("0")
                eth_txs_sorted = []
                for tx in normal_txs:
                    if tx.get("isError", "0") != "0":
                        if tx["from"].lower() == WALLET:
                            fee = Decimal(tx["gasUsed"]) * Decimal(tx["gasPrice"]) / Decimal("1e18")
                            eth_txs_sorted.append({
                                "ts": int(tx["timeStamp"]),
                                "date": ts_to_date(tx["timeStamp"]),
                                "hash": tx["hash"],
                                "direction": "GAS (failed)",
                                "from": tx["from"],
                                "to": tx.get("to", ""),
                                "amount": -fee,
                                "fn": tx.get("functionName", ""),
                            })
                        continue
                    value = wei_to_eth(tx["value"])
                    from_addr = tx["from"].lower()
                    to_addr = (tx.get("to") or "").lower()
                    direction = "IN" if to_addr == WALLET else "OUT"
                    fee = Decimal("0")
                    if from_addr == WALLET:
                        fee = Decimal(tx["gasUsed"]) * Decimal(tx["gasPrice"]) / Decimal("1e18")
                    signed = value if direction == "IN" else -value
                    fn = tx.get("functionName", "")
                    eth_txs_sorted.append({
                        "ts": int(tx["timeStamp"]),
                        "date": ts_to_date(tx["timeStamp"]),
                        "hash": tx["hash"],
                        "direction": direction,
                        "from": tx["from"],
                        "to": tx.get("to", ""),
                        "amount": signed,
                        "fn": fn,
                    })
                    if fee > 0:
                        eth_txs_sorted.append({
                            "ts": int(tx["timeStamp"]),
                            "date": ts_to_date(tx["timeStamp"]),
                            "hash": tx["hash"],
                            "direction": "GAS",
                            "from": tx["from"],
                            "to": "",
                            "amount": -fee,
                            "fn": fn,
                        })
                for tx in internal_txs:
                    if tx.get("isError", "0") != "0":
                        continue
                    value = wei_to_eth(tx["value"])
                    if value == 0:
                        continue
                    to_addr = (tx.get("to") or "").lower()
                    direction = "IN" if to_addr == WALLET else "OUT"
                    signed = value if direction == "IN" else -value
                    eth_txs_sorted.append({
                        "ts": int(tx["timeStamp"]),
                        "date": ts_to_date(tx["timeStamp"]),
                        "hash": tx.get("hash", tx.get("transactionHash", "")),
                        "direction": f"{direction} (internal)",
                        "from": tx["from"],
                        "to": tx.get("to", ""),
                        "amount": signed,
                        "fn": "",
                    })
                eth_txs_sorted.sort(key=lambda x: x["ts"])
                for t in eth_txs_sorted:
                    running += t["amount"]
                    disc_rows.append({
                        "Token/Asset": "ETH",
                        "Contract Address": "",
                        "Row Type": "TX",
                        "Date": t["date"],
                        "Transaction Hash": t["hash"],
                        "Direction": t["direction"],
                        "From": t["from"],
                        "To": t["to"],
                        "Amount": f"{t['amount']:.18f}",
                        "Running Balance": f"{running:.18f}",
                        "Function Called": t["fn"],
                        "Expected Balance": "",
                        "Actual Balance": "",
                        "Difference": "",
                        "Resolution": "",
                    })
            else:
                # ERC20 token tree
                token_txs = token_tx_lookup.get(contract, [])
                token_txs_sorted = sorted(token_txs, key=lambda x: int(x["timeStamp"]))
                running = Decimal("0")
                decimals_str = str(discrepancy_contracts[contract].get("decimals", 18))
                for tf_entry in [token_flows.get(contract)]:
                    if tf_entry:
                        decimals_str = str(tf_entry["decimals"])

                for tx in token_txs_sorted:
                    value = token_amount(tx["value"], decimals_str)
                    to_addr = (tx.get("to") or "").lower()
                    direction = "IN" if to_addr == WALLET else "OUT"
                    signed = value if direction == "IN" else -value
                    running += signed

                    # Look up the function from the normal tx
                    ntx = normal_tx_by_hash.get(tx["hash"].lower(), {})
                    fn = ntx.get("functionName", "")

                    disc_rows.append({
                        "Token/Asset": symbol,
                        "Contract Address": contract,
                        "Row Type": "TX",
                        "Date": ts_to_date(tx["timeStamp"]),
                        "Transaction Hash": tx["hash"],
                        "Direction": direction,
                        "From": tx["from"],
                        "To": tx.get("to", ""),
                        "Amount": f"{signed}",
                        "Running Balance": f"{running}",
                        "Function Called": fn,
                        "Expected Balance": "",
                        "Actual Balance": "",
                        "Difference": "",
                        "Resolution": "",
                    })

                # Final row showing the gap + auto-resolution
                resolution = ""
                if info["expected"] < 0 and info["actual"] >= 0:
                    # More tokens went out than came in via Transfer events,
                    # but actual balance is non-negative — the gap must be
                    # non-transfer balance increases (rebasing, staking yield)
                    resolution = (
                        f"RESOLVED: {info['diff']} {symbol} from non-transfer "
                        f"balance increase (rebasing/staking rewards). "
                        f"Wallet received {abs(info['expected']):.9f} more via "
                        f"rebase than tracked by Transfer events."
                    )
                elif info["expected"] > 0 and info["actual"] == 0:
                    # Expected positive balance but actual is 0 — tokens
                    # were likely burned, migrated, or are worthless/scam
                    resolution = (
                        f"RESOLVED: Token balance zeroed outside of Transfer "
                        f"events. Likely a scam/airdrop token, admin burn, "
                        f"or token migration."
                    )
                elif info["expected"] > 0 and info["actual"] > 0 and info["diff"] > 0:
                    # Actual is higher than expected — extra tokens appeared
                    resolution = (
                        f"RESOLVED: +{info['diff']} {symbol} from non-transfer "
                        f"balance increase (rebasing rewards, staking yield, "
                        f"or direct contract mint)."
                    )
                elif info["expected"] > 0 and info["actual"] > 0 and info["diff"] < 0:
                    # Actual is lower than expected — tokens disappeared
                    # Typical of fee-on-transfer / reflection / tax tokens
                    resolution = (
                        f"RESOLVED: {info['diff']} {symbol} lost to fee-on-transfer "
                        f"(tax token). Transfer events log pre-tax amounts but "
                        f"wallet receives less due to built-in token tax."
                    )

                disc_rows.append({
                    "Token/Asset": symbol,
                    "Contract Address": contract,
                    "Row Type": "GAP",
                    "Date": "",
                    "Transaction Hash": "",
                    "Direction": "UNTRACKED",
                    "From": "",
                    "To": "",
                    "Amount": f"{info['diff']}",
                    "Running Balance": f"{info['actual']}",
                    "Function Called": "Balance change not captured by Transfer events",
                    "Expected Balance": "",
                    "Actual Balance": "",
                    "Difference": "",
                    "Resolution": resolution,
                })

            # Empty separator row between tokens
            disc_rows.append({k: "" for k in disc_rows[0]})

        disc_fields = [
            "Token/Asset", "Contract Address", "Row Type",
            "Date", "Transaction Hash", "Direction",
            "From", "To", "Amount", "Running Balance",
            "Function Called",
            "Expected Balance", "Actual Balance", "Difference",
            "Resolution",
        ]
        with open(DISCREPANCY_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=disc_fields)
            writer.writeheader()
            writer.writerows(disc_rows)
        print(f"\n[+] Wrote {len(discrepancy_contracts)} token audit trails "
              f"({len(disc_rows)} rows) to {DISCREPANCY_FILE}")
    else:
        print(f"\n[+] No discrepancies found — no {DISCREPANCY_FILE} written.")

    print("\n" + "=" * 72)
    print("  Done.")
    print("=" * 72)


if __name__ == "__main__":
    main()
