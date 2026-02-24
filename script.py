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

    discrepancies = []

    # ETH
    diff_eth = actual_eth - expected_eth
    print(f"\n{'ETH':>10}")
    print(f"  {'Expected balance':30s} {expected_eth:>28.18f}")
    print(f"  {'Actual on-chain balance':30s} {actual_eth:>28.18f}")
    print(f"  {'Difference':30s} {diff_eth:>28.18f}")
    if abs(diff_eth) > Decimal("0.000000000001"):
        print(f"  ** DISCREPANCY DETECTED **")
        discrepancies.append({
            "Token/Asset": "ETH",
            "Contract Address": "",
            "Expected Balance": f"{expected_eth:.18f}",
            "Actual Balance": f"{actual_eth:.18f}",
            "Difference": f"{diff_eth:.18f}",
            "Total Inflows": f"{eth_in:.18f}",
            "Total Outflows": f"{eth_out:.18f}",
            "Total Gas Fees": f"{gas_total:.18f}",
        })
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
            discrepancies.append({
                "Token/Asset": symbol,
                "Contract Address": contract,
                "Expected Balance": f"{expected}",
                "Actual Balance": f"{actual}",
                "Difference": f"{diff}",
                "Total Inflows": f"{tf['in']}",
                "Total Outflows": f"{tf['out']}",
                "Total Gas Fees": "0",
            })
        else:
            print(f"  OK")

    # ---- Write discrepancy CSV ----
    if discrepancies:
        disc_fields = [
            "Token/Asset", "Contract Address",
            "Expected Balance", "Actual Balance", "Difference",
            "Total Inflows", "Total Outflows", "Total Gas Fees",
        ]
        with open(DISCREPANCY_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=disc_fields)
            writer.writeheader()
            writer.writerows(discrepancies)
        print(f"\n[+] Wrote {len(discrepancies)} discrepancies to {DISCREPANCY_FILE}")
    else:
        print(f"\n[+] No discrepancies found — no {DISCREPANCY_FILE} written.")

    print("\n" + "=" * 72)
    print("  Done.")
    print("=" * 72)


if __name__ == "__main__":
    main()
