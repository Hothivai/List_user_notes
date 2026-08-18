from datetime import datetime, timezone, timedelta
import json
from decimal import Decimal, getcontext
# import orjson
import requests
import time
import pytz
from collections import defaultdict
from eth_abi import decode
from eth_utils import to_bytes

# from db import get_connection

getcontext().prec = 50

# Api urls of blockchains v2
API_URL = 'https://api.etherscan.io/v2/api'
API_KEY = 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U'
# List chain id of evm blocks
CHAIN_ID = {
  "ETH": 1,
  "BAS": 8453,
  "BSC": 56,
  "POL": 137,
  "ARB": 42161,
  "LIN": 59144
}

CURRENCY_MAP = {
  'ETH': 'ETH',
  'BSC': 'BNB',
  'POL': 'POL',
  'ARB': 'ARB',
  'BAS': 'ETH',
  'LIN': 'ETH'
}

'''
    old type api endpoint
    tx  internaltx  tokentxns   tokennfttx
    new type api endpoint
    txlist  txlistinternal  tokentx   tokennfttx
'''

MAPPING_DATA = {
  'transaction': {
    'type': 'txlist',
    'fields': [
      'block',
      'hash',
      'from_address',
      'to_address',
      'contract',
      'tx_time',
      'is_error',
      'amount',
      'internal_transaction',
      'erc20_token',
      'nft',
      'input',
    ],
    'mapping': {
      'block': 'blockNumber',
      'hash': 'hash',
      'from_address': 'from',
      'to_address': 'to',
      'contract':'contractAddress',
      'tx_time': 'timeStamp',
      'is_error': 'isError',
      'amount': 'value',
      'internal_transaction': None,
      'erc20_token': None,
      'nft': None,
      'input': 'input'
    }
  },
  'internal_transaction': {
    'type': 'txlistinternal',
    'fields': [
      'hash',
      'block',
      'from_address',
      'to_address',
      'amount',
      'contract',
    ],
    'mapping': {
      'hash': 'hash',
      'block': 'blockNumber',
      'from_address': 'from',
      'to_address': 'to',
      'amount': 'value',
      'contract':'contractAddress'
    }
  },
  'erc20_token': {
    'type': 'tokentx',
    'fields': [
      'hash',
      'block',
      'from_address',
      'to_address',
      'contract',
      'amount',
      'token_name',
      'symbol',
      'decimal'
    ],
    'mapping': {
      'hash': 'hash',
      'block': 'blockNumber',
      'from_address': 'from',
      'to_address': 'to',
      'contract': 'contractAddress',
      'amount': 'value',
      'token_name': 'tokenName',
      'symbol': 'tokenSymbol',
      'decimal': 'tokenDecimal',

    }
  },
  'erc721_token': {
    'type': 'tokennfttx',
    'fields': [
      'hash',
      'block',
      'from_address',
      'to_address',
      'token_id',
      'token_name',
      'symbol',
      'contract',
      'decimal'
    ],
    'mapping': {
      'hash': 'hash',
      'block': 'blockNumber',
      'from_address': 'from',
      'to_address': 'to',
      'token_id': 'tokenID',
      'token_name': 'tokenName',
      'symbol': 'tokenSymbol',
      'contract': 'contractAddress',
      'decimal': 'tokenDecimal',
    }
  }
}

FUNCTION_SELECTORS = {
    "0x219f5d17": {  # increaseLiquidity((uint256,uint256,uint256,uint256,uint256,uint256))
        "name": "increaseLiquidity",
        "types": ['(uint256,uint256,uint256,uint256,uint256,uint256)'],
        "is_tuple": True,
        "token_index": 0,
    },
    "0xfc6f7865": {  # collect((uint256,address,uint128,uint128))
        "name": "collect",
        "types": ['(uint256,address,uint128,uint128)'],
        "is_tuple": True,
        "token_index": 0,
    },
    "0x18fccc76": {  # harvest(uint256,address)
        "name": "harvest",
        "types": ['uint256', 'address'],
        "is_tuple": False,
        "token_index": 0,
    },
}


def decode_tx_input(input_data: str):
    """
    Decode tx input data to extract tokenId and parameters.
    Works for increaseLiquidity, collect, and harvest functions.
    """
    if not input_data or len(input_data) < 10:
        return None

    selector = input_data[:10]
    data = input_data[10:]

    if selector not in FUNCTION_SELECTORS:
        return None

    func = FUNCTION_SELECTORS[selector]
    types = func["types"]
    is_tuple = func.get("is_tuple", False)

    try:
        # Convert hex -> bytes
        data_bytes = to_bytes(hexstr=data)

        # Decode ABI params
        decoded = decode(types, data_bytes)

        # unwrap tuple if needed
        if is_tuple:
            decoded = decoded[0]

        token_id = decoded[func["token_index"]]
        return {
            "function": func["name"],
            "token_id": int(token_id),
            "params": decoded
        }

    except Exception as e:
        print(f"Decode error: {e}")
        return None

def normalize_value(value: Decimal):
  format_value = format(Decimal(value), 'f')
  return format_value if format_value != "" else "0"


def convert_block_time(block_time: str, tz: str = "UTC", format_time: str = "%Y-%m-%d %H:%M:%S"):
  convert_time = datetime.fromtimestamp(int(block_time), tz=timezone.utc)

  if tz.upper() == "VN":
    return convert_time.astimezone(pytz.timezone("Asia/Ho_Chi_Minh")).strftime(format_time)
  return convert_time.strftime(format_time)


def convert_datetime_to_blocktime(date_time: str, add_time: bool = False):
  date_time = f"{date_time} 00:00:00"
  dt = datetime.strptime(date_time, "%Y-%m-%d %H:%M:%S")
  dt = dt.replace(tzinfo=timezone.utc)
  if add_time:
    dt = dt + timedelta(days=1)
  block_time = int(dt.timestamp())
  return block_time


def generate_params(chain_id: int, module: str, actions: str, closest: str, timestamp: int, apikey: str):
  params = {
    "chainid": chain_id,
    "module": module,
    "action": actions,
    "closest": closest,
    "timestamp": timestamp,
    "apikey": apikey,
  }
  return params

def prioritize_token(tokens:list, symbol:str):
  symbol_upper = symbol.upper()
  
  sorted_tokens = sorted(tokens, key=lambda x: (x.get("symbol", "").upper() != symbol_upper, x.get("contract", "")==""))
  return sorted_tokens

def calculate_separate_tokens(transactions:list, symbol:str):
  totals = defaultdict(lambda:{"sent": 0.0, "received": 0.0, "total": 0.0})
  for transaction in transactions:
    detail_tokens = transaction.get("details",[])
    for token in detail_tokens:
      symb = token.get("symbol","")
      amount = float(token.get("amount",0))
      if amount >= 0:
        totals[symb]["received"] = float(totals.get(symb,{}).get("received",0.0)) + amount
      else:
        totals[symb]["sent"] = float(totals.get(symb,{}).get("sent",0.0)) + amount
      totals[symb]["total"] = float(totals.get(symb,{}).get("total",0.0)) + amount
      
  # sorted totals by symbol
  sorted_totals = dict(sorted(totals.items(), key=lambda token:(
    0 if token[0].lower() == symbol.lower() else 1,
    token[0].lower()
  )))
  
  return sorted_totals

def safe_fetch_with_retry(url, params= None, max_retries=100, delay=2):
  for attempt in range(max_retries):
    try:
      response = requests.get(url, params=params, timeout=10)
      data = response.json()
      
      # check if the response is successful
      if data.get("status")==1 or "Free API access" not in str(data.get("result","")):
        return data
      
      # print(data.get("result",""))
      # print(f"Retry {attempt + 1}/{max_retries} after unsuccessful response")
      time.sleep(delay)
      
    except requests.RequestException as e:
      print(f"Retry {attempt + 1}/{max_retries} after exception: {e}")
      time.sleep(delay)
  
  # print("❌ Failed after multiple retries.")
  return None

def process_transaction_transfer(transaction:dict, wallet: str, type:str, field_to_key:list):
  tx = {
        field: transaction[key] if key else []
        for field, key in field_to_key
      }

  from_address = tx["from_address"].lower()
  to_address = tx["to_address"].lower()

  # Normalize amount for tx / internaltx
  if type in ['txlist', 'txlistinternal']:
    tx["amount"] = normalize_value(
      Decimal(tx["amount"]) / Decimal(10 ** 18)
    )

  # Extra processing for normal transactions
  if type == "txlist":
    tx["tx_time"] = convert_block_time(tx["tx_time"])
    tx["tx_fee"] = normalize_value(
      Decimal(transaction["gasUsed"]) * Decimal(transaction["gasPrice"]) / Decimal(10 ** 18)
    )

  # Processing for ERC20 token transfers
  if type == "tokentx":
    tx["amount"] = normalize_value(
      Decimal(tx["amount"]) / Decimal(10 ** int(tx["decimal"]))
    )

  # Add direction info (IN / OUT / SELF)
  if type in ["txlist", "txlistinternal", "tokentx", "tokennfttx"]:
    tx["direct"] = (
      "IN" if from_address != wallet and to_address == wallet
      else "SELF" if from_address == wallet and to_address == wallet
      else "OUT"
    )

  # Adjust amount for OUT going ERC20
  if type in ["txlist", "txlistinternal", "tokentx"] and tx["direct"] == "OUT" and tx["amount"] != "0":
    tx["amount"] = -float(tx["amount"])

  # Attach wallet info for token / nft transactions
  if type in ["txlistinternal", "tokentx", "tokennfttx", "txlist"]:
    if tx["direct"] in ["OUT", "SELF"]:
      tx["wallet"] = tx["to_address"].lower()
    elif tx["direct"] == "IN":
      tx["wallet"] = tx["from_address"].lower()

  return tx

def get_transactions(wallet_address: str, chain_id: int, module: str, action: str, start_block: int = 0,
                     end_block: int = 99999999, mapping_type: dict = None, return_block: bool = False):
  page = 1
  offset = 1000
  transactions = []

  # pre-catched mapping
  field_to_key = [(field, mapping_type['mapping'][field]) for field in mapping_type['fields']]
  while True:

    start_time = time.time()

    params = {
      "chainid": chain_id,
      "module": module,
      "action": action,
      "address": wallet_address,
      "startblock": start_block,
      "endblock": end_block,
      "page": page,
      "offset": offset,
      "apikey": API_KEY,
    }

    data = safe_fetch_with_retry(API_URL, params)
    
    if data is None:
      break
    if not data["result"]:
      break
    wallet_address_lower = wallet_address.lower()
    for tx in data["result"]:
      transaction = process_transaction_transfer(tx, wallet_address_lower, mapping_type["type"], field_to_key)
      transactions.append(transaction)

    page += 1
    ellapsed = time.time() - start_time
    time.sleep(max(0.0, 0.3 - ellapsed))

  if return_block and len(transactions) == 0:
    return transactions, 0, 0
  elif return_block and len(transactions) > 0:
    blocks = [int(tx["block"]) for tx in transactions]

    start_block = min(blocks)
    end_block = max(blocks)

    return transactions, start_block, end_block

  return transactions


# filter transaction from 1756227600-2025-08-26 17:00:00 to 1756314000-2025-08-27 17:00:00
def fetch_detail_transactions(wallet_address: str, chain: str, start_time: str, end_time: str):
    chain_id = CHAIN_ID[chain]
    block_time_start = convert_datetime_to_blocktime(start_time)
    block_time_end = convert_datetime_to_blocktime(end_time, add_time=True)

    # get start block and end block
    start_block_params = generate_params(chain_id, "block", "getblocknobytime", "after", block_time_start, API_KEY)
    end_block_params = generate_params(chain_id, "block", "getblocknobytime", "before", block_time_end, API_KEY)
    response_start_block = safe_fetch_with_retry(API_URL, params=start_block_params)
    response_end_block = safe_fetch_with_retry(API_URL, params=end_block_params)
    start_block = response_start_block["result"]
    print(f"- Start block: {start_block}")
    end_block = response_end_block["result"]
    print(f"- End block: {end_block}")

    # get all transactions
    list_tx, start_block, end_block = get_transactions(
        wallet_address, chain_id, "account", "txlist",
        start_block, end_block, MAPPING_DATA["transaction"], True
    )

    if len(list_tx) == 0:
        return []

    transactions = {}
    for tx in list_tx:
        transactions[tx["hash"]] = tx
        transactions[tx["hash"]]["chain"] = chain
        transactions[tx["hash"]]["internal_transaction"] = []
        transactions[tx["hash"]]["erc20_token"] = []
        transactions[tx["hash"]]["nft"] = []
        transactions[tx["hash"]]["input"] = tx["input"] or ""

        if float(tx["amount"]) != 0:
            transactions[tx["hash"]]["internal_transaction"].append({
                "hash": tx["hash"],
                "from_address": tx["from_address"],
                "to_address": tx["to_address"],
                "amount": tx["amount"],
                "direct": tx["direct"],
                "symbol": CURRENCY_MAP[chain],
                "wallet": tx["to_address"],
                "contract": tx["contract"],
                "input": tx["input"],
            })

    list_transaction_keys = set(transactions.keys())

    # ========== INTERNAL TX ==========
    internal_txs = get_transactions(wallet_address, chain_id, "account", "txlistinternal",
                                    start_block, end_block, MAPPING_DATA["internal_transaction"], False)
    if internal_txs:
        for itx in internal_txs:
            h = itx["hash"]
            if h not in transactions:
                continue
            if float(transactions[h]["amount"]) != 0:
                continue
            itx["symbol"] = CURRENCY_MAP[chain]
            transactions[h]['internal_transaction'].append(itx)
    time.sleep(0.2)

    # ========== ERC20 TOKEN ==========
    erc20_token = get_transactions(wallet_address, chain_id, "account", "tokentx",
                                   start_block, end_block, MAPPING_DATA["erc20_token"], False)
    if erc20_token:
        for etx in erc20_token:
            if etx["hash"] in list_transaction_keys:
                transactions[etx["hash"]]['erc20_token'].append(etx)
    time.sleep(0.2)

    # ========== ERC721 TOKEN ==========
    erc721_token = get_transactions(wallet_address, chain_id, "account", "tokennfttx",
                                    start_block, end_block, MAPPING_DATA["erc721_token"], False)

    unique_nfts = []
    if erc721_token:
        seen_pairs = set()
        for tx in erc721_token:
            pair = (tx["hash"], tx["token_id"])
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                unique_nfts.append(tx)

        for etx in unique_nfts:
            if etx["hash"] in list_transaction_keys:
                transactions[etx["hash"]]['nft'] = etx

    # ========== BỔ SUNG NFT TỪ DECODE ==========
    decoded_nfts = []
    for tx in list_tx:
        decoded = decode_tx_input(tx.get("input", ""))
        if not decoded or decoded.get("token_id") is None:
            continue

        tx_hash = tx["hash"]
        token_id = str(decoded["token_id"])

        # Nếu tx chưa có NFT nào chứa token_id này
        has_nft = any(n["token_id"] == token_id for n in transactions[tx_hash]["nft"])
        if not has_nft:
          if wallet_address == tx.get("to_address"):
            direct = "IN"
            wallet = tx.get("from_address")
          else:
            direct = "OUT"
            wallet = tx.get("to_address")

          new_nft = {
              "hash": tx_hash,
              "from_address": tx.get("from_address"),
              "to_address": tx.get("to_address"),
              "token_id": token_id,
              "token_name": "Unknown",
              "symbol": "Unknown",
              "contract": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
              "decimal": 0,
              "direct": direct,
              "wallet": wallet,
          }
          transactions[tx_hash]['nft'] = new_nft
          # decoded_nfts.append(new_nft)
          print(f"[+] Added NFT from decode: token_id={token_id}, func={decoded['function']}")

    # print(f"→ Found {len(decoded_nfts)} decoded NFTs")
    # print(f"→ Total transactions: {len(transactions)}")

    return transactions

def merge_internal_tx(wallet:str, internal: list):
  merged = {}
  for tx in internal:
    key = (tx["hash"], tx["from_address"], tx["to_address"])
    reverse_key = (tx["hash"], tx["to_address"], tx["from_address"])
    
    if key in merged:
      merged[key]["amount"] += float(tx["amount"])
      
    elif reverse_key in merged:
      merged[reverse_key]["amount"] += float(tx["amount"])
      
    else:
      merged[key] = {
        **tx,
        "amount": float(tx["amount"])
      }
    
  # after merging, redefine direct and wallet
  results = []
  for tx in merged.values():
    from_address = tx["from_address"].lower()
    to_address = tx["to_address"].lower()
    wallet_lower = wallet.lower()
    
    if float(tx["amount"]) > 0 and from_address == wallet_lower:
      tx["from_address"] = to_address
      tx["to_address"] = from_address
      tx["wallet"] = from_address
      tx["direct"] = "IN"
    elif float(tx["amount"]) < 0 and from_address != wallet_lower:
      tx["from_address"] = to_address
      tx["to_address"] = from_address
      tx["wallet"] = from_address
      tx["direct"] = "OUT"

    results.append(tx)
  return results

def get_transactions_with_filter(wallet_address: str, chains: list[str], start_time: str, end_time: str, symbol: str,
                                 contract: str):
  transactions = []
  max_length = 0

  for chain in chains:
    res_tx = fetch_detail_transactions(wallet_address, chain, start_time, end_time)

    # filter fetched data with symbol and contract
    filter_transaction = {}
    
    # check if no transaction
    if not res_tx:
      print("No transactions found")
      continue
    
    for tx_hash, tx in res_tx.items():
      res_tx[tx_hash]["chain"] = chain
      merge_tx = tx.get("internal_transaction",[]) + tx.get("erc20_token",[])
      for etx in merge_tx:
        if contract:
  
          if etx["symbol"].lower() == symbol.lower() and etx["contract"].lower() == contract.lower():
            filter_transaction[tx_hash] = tx
            continue
        else:
          if etx["symbol"].lower() == symbol.lower():
            filter_transaction[tx_hash] = tx
            continue
  
    # convert tranaction to array
    convert_transaction = list(filter_transaction.values())
    transactions.extend(convert_transaction)

  # find max length of erc20_token
  for tx in transactions:
    merge_internal = merge_internal_tx(wallet_address, tx["internal_transaction"])
    merge_token = merge_internal + tx["erc20_token"]
    prioritized_tokens = prioritize_token(merge_token,symbol)
    tx["details"] = prioritized_tokens
    del tx["internal_transaction"]
    del tx["erc20_token"]
    
    # if tx["internal_transaction"]:
    #   internal_tx_flag = True
    if len(tx["details"]) > max_length:
      max_length = len(tx["details"])
  separate_total_symbol = calculate_separate_tokens(transactions, symbol)
  return {"transactions": transactions, "max_length": max_length, "total":separate_total_symbol}
