import json
import os
import time
from collections import defaultdict
from base58 import b58decode, b58encode
from base64 import b64decode
from typing import Dict, Any, List
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from construct import Struct, Int8ul, Int64ul, Bytes, Array, Int32sl, Int64sl
import requests
from dotenv import load_dotenv
from decimal import Decimal, getcontext
import struct

from services.transaction_history_v2.tx_his_v2 import (convert_block_time, convert_datetime_to_blocktime, calculate_separate_tokens)
from logging_setup import tx_history_logger as logger

# ==================== CONFIG ==================== #
load_dotenv(verbose=True)
getcontext().prec = 50

# HELIUS_API_KEY = "bd2bcd81-1022-4de8-998a-f756addb8895"
HELIUS_API_KEY = "338c8817-2f81-4694-ad08-abd9078c6db3"
METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=338c8817-2f81-4694-ad08-abd9078c6db3"
JUPITER_PROGRAM_IDS = {
  "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
}

# ==================== LAYOUT ==================== #
Int128ul = Struct(
  "low" / Int64ul,
  "high" / Int64ul
)

Int128sl = Struct(
  "low"/Int64sl,
  "high"/Int64sl
)
POSITION_REWARD_INFO = Struct(
  "growth_inside_last_x64"/Int128ul,
  "reward_amount_owed"/Int64ul
)
PERSONAL_POSITION_STATE_LAYOUT = Struct(
  "bump" / Array(1, Int8ul),
  "nft_mint" / Bytes(32),
  "pool_id" / Bytes(32),
  "tick_lower_index"/Int32sl,
  "tick_upper_index"/Int32sl,
  "liquidity"/Int128ul,
  "fee_growth_inside_0_last_x64"/Int128ul,
  "fee_growth_inside_1_last_x64"/Int128ul,
  "token_fees_owed_0"/Int64ul,
  "token_fees_owed_1"/Int64ul,
  "reward_infos"/Array(3,POSITION_REWARD_INFO),
  "recent_epoch"/Int64ul,
  "padding"/Array(7,Int64ul)
)

# ==================== DISCRIMINATOR MAP ==================== #
discriminators = {
  "3a7fbc3e4f52c460":{
    "name":"decrease_liquidity_v2", # Pancake Swap
    "accounts":None
  },
  "851d59df45eeb00a":{
    "name":"increase_liquidity_v2", # Pancake Swap
    "accounts":None
  },
  "4dffae527d1dc92e":{
    "name":"open_position_with_token22_nft", # Pancake Swap
    "accounts":None
  },
  "a860b7a35c0a28a0":{
    "name":"fill",
    "accounts": None
  },
  "c1209b3341d69c81":{
    "name":"shared_accounts_route", # Jupiter Aggregator v6
    "fee":True,
    "accounts":None
  },
  "d19853937cfed8e9":{
    "name":"shared_accounts_route_v2",
    "fee":False,
    # "fee_index":-2,
    "accounts":[
      {
        "list_field":["from_token_address","from_address","contract"],
        "index":{
          "from_token_address":2,
          "from_address":1,
          "contract":6
        }
      },
      {
        "list_field":["to_token_address","to_address","contract"],
        "index":{
          "to_token_address":5,
          "to_address":1,
          "contract":7
        }
      },      
    ]
  },
  "bb64facc31c4af14":{
    "name":"route_v2", # Jupiter Aggregator v6
    "checkTransfer":True,
    "fee":True,
    "fee_index":-1,
    "accounts":[
      {
        "list_field":["from_token_address","from_address","contract"],
        "index":{
          "from_token_address":1,
          "from_address":0,
          "contract":3
        } 
      },
      {
        "list_field":["to_token_address","to_address","contract"],
        "index":{
          "to_token_address":2,
          "to_address":0,
          "contract":4
        }
      },      
    ]
  },
  "e517cb977ae3ad2a":{
    "name":"route",  # Jupiter Aggregator v6
    "fee":True,
    "fee_account":6,
    "accounts":None  # xxx
  },
  "0ebf2cf68ee1e09d":{
    "name":"swap_tob_v3", # OKX DEX: Aggregation Router V2
    "accounts":None
  },
  "7b86510031446262":{
    "name":"close_position",
  }
}

# ==================== SAFE FETCH DATA ==================== #
def safe_fetch_with_retry(url, params = None, max_retries=5, delay=1):
  for attempt in range(max_retries):
    try:
      response = requests.get(url, params=params, timeout=(5,30))
      if response.status_code != 200:
        logger.warning("[SOL] Helius API Error: status=%d, response=%s", response.status_code, response.text)
        time.sleep(delay)
        continue
      data = response.json()
      return data
    except requests.RequestException as e:
      logger.warning("[SOL] Helius API retry %d/%d: %s", attempt + 1, max_retries, e)
      time.sleep(delay)
  
  return None

def safe_rpc_post_with_retry(method: str, params = None, max_retries=5, delay=1):
  params = params or []
  payload = {
    "jsonrpc": "2.0",
    "id": method,
    "method": method,
    "params": params
  }
  for attempt in range(max_retries):
    try:
      response = requests.post(RPC_URL, json=payload, timeout=(5,30))
      if response.status_code != 200:
        logger.warning("[SOL] RPC Error: status=%d, response=%s", response.status_code, response.text)
        time.sleep(delay)
        continue
      data = response.json()
      if data.get("error"):
        logger.warning("[SOL] RPC returned error for %s: %s", method, data.get("error"))
        time.sleep(delay)
        continue
      return data.get("result")
    except requests.RequestException as e:
      logger.warning("[SOL] RPC retry %d/%d: %s", attempt + 1, max_retries, e)
      time.sleep(delay)
  
  return None

# ==================== SYMBOL CACHE ==================== #
nfts = {}

from services.excute_transaction_v2 import get_symbol_cache_from_db
symbol_cache = get_symbol_cache_from_db()


# ==================== HELPER FUNCTIONS AND METADATA ==================== #
def get_metadata_pda(mint_address: str):
  mint_pubkey = Pubkey.from_string(mint_address)
  seeds = [
    b"metadata",
    bytes(METADATA_PROGRAM_ID),
    bytes(mint_pubkey)
  ]
  metadata_pubkey, _ = Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)
  return metadata_pubkey

def read_string(data, offset):
  str_len = struct.unpack_from("<I", data, offset)[0]
  offset += 4
  value = struct.unpack_from(f"<{str_len}s", data, offset)[0].decode("utf-8")
  offset += str_len
  return value.strip("\x00"), offset

def decode_metadata(data: bytes):
  offset = 0
  key = struct.unpack_from("<B", data, offset)[0]
  offset += 1
  
  update_authority = struct.unpack_from("<32s", data, offset)[0].hex()
  offset += 32
  auth = bytes.fromhex(update_authority)
  auth_pubkey = Pubkey.from_bytes(auth)

  mint = struct.unpack_from("<32s", data, offset)[0].hex()
  offset += 32
  mint_bytes = bytes.fromhex(mint)
  mint_pubkey = Pubkey.from_bytes(mint_bytes)
  
  name, offset = read_string(data, offset)
  symbol, offset = read_string(data, offset)
  uri, offset = read_string(data, offset)
  
  seller_fee_basis_points = struct.unpack_from("<H", data, offset)[0]
  offset += 2
  
  has_creator = struct.unpack_from("<?", data, offset)[0]
  offset += 1
  
  primary_sale_happened = struct.unpack_from("<?", data, offset)[0]
  offset += 1
  
  is_mutable = struct.unpack_from("<?", data, offset)[0]
  offset += 1
  
  return {
    "key": key,
    "update_authority": auth_pubkey,
    "mint": mint_pubkey,
    "name": name,
    "symbol": symbol,
    "uri": uri,
    "seller_fee_basis_points": seller_fee_basis_points,
    "has_creator": has_creator,
    "primary_sale_happened": primary_sale_happened,
    "is_mutable": is_mutable
  }


def get_metadata( metadata_key):
  client = Client(RPC_URL)
  meta_account = get_metadata_pda(metadata_key)
  meta_response = client.get_account_info(meta_account)
  if meta_response.value is None:
    logger.warning("[SOL] Metadata not found for mint address %s", metadata_key)
    return {"symbol": "XXX"}
  raw_data = meta_response.value.data
  
  extract_data = decode_metadata(raw_data)
  
  return extract_data

def get_symbol(mint_address:str):
  if mint_address in symbol_cache:
    return symbol_cache[mint_address]

  meta_data = get_metadata(mint_address)
  symbol = meta_data["symbol"]
  symbol_cache[mint_address] = symbol
  return symbol

def parse_discriminator(data:str):
  raw_data= b58decode(data)
  
  return raw_data[:8].hex()

def get_nft_mint_from_persional_position(address:str):
  client = Client(RPC_URL)
  pubKey = Pubkey.from_string(address)
  
  resp = client.get_account_info(pubKey)
  account_info = resp.value
  if account_info is None:
    return None
  
  data = account_info.data
  parsed = PERSONAL_POSITION_STATE_LAYOUT.parse(data[8:])
  nft_mint = b58encode(parsed["nft_mint"]).decode()
  return nft_mint

def get_nft_mint_from_close_position(list_account:list, nfts: dict, ):
  persional_position = list_account[3]
  nft_mint = list_account[1]
  nft_account = list_account[2]
  
  nfts[persional_position] = {
    "token_id": nft_mint,
    "wallet": nft_account
  }
  

def decode_transfer_token(data:bytes, accounts:list):
  instr, amount = struct.unpack_from("<BQ", data,0)
  index_accounts = {
    "from_token_address":accounts[0],
    "to_token_address":accounts[1],
    "from_address":accounts[2]
  }

  return amount, index_accounts
  
def decode_transfer_system(data: bytes, accounts:list):
  instr, amount = struct.unpack_from("<IQ", data,0)
  index_accounts = {
    
  }
  
  return amount, index_accounts  

def remove_intermediate_tokens(token_transfers:list, changed_token:dict):
  remove_token = []
  for tx in token_transfers:
    symbol = tx.get("symbol","")
    if symbol in changed_token:
      remove_token.append(tx)
  return remove_token

def remove_fee_transfer(token_transfers:list):
  results = [tx for tx in token_transfers if not tx.get("is_fee", False)]
  return results

def remove_context_transfers(token_transfers:list):
  return [tx for tx in token_transfers if not tx.get("is_context", False)]

def merge_token_transfers(token_transfers: list, wallet_address:str):
  merged = {}
  for tx in token_transfers:
    key = (tx["from_token_address"], tx["to_token_address"], tx["from_address"],tx["to_address"], tx["contract"])
    if key not in merged:
      merged[key] = {
        **tx
      }
    else:
      merged[key]["amount"] += tx["amount"]
  result_tokens = merged.values()
  for token in result_tokens:
    token["wallet"] = token["to_address"] if token["from_address"]==wallet_address else token["from_address"]
  return result_tokens

def prioritize_token(tokens:list, symbol:str, mint:str):
  symbol_upper = symbol.upper()
  mint_lower = mint.lower()
  if mint_lower != "":
    sorted_tokens = sorted(
      tokens, key = lambda x: (
        x.get("is_fee",False),
        x.get("contract").lower()!=mint_lower,
        x.get("symbol","").upper()!=symbol_upper
      )
    )
  else:
    sorted_tokens = sorted(
      tokens, key = lambda x: (
        x.get("is_fee",False),
        x.get("symbol","").upper()!=symbol_upper
      )
    )
  return sorted_tokens

def get_length_token_transfers(transactions:list):
  if not transactions:
    return 0
  token_length = len(transactions)
  if any(tx.get("is_fee", False) for tx in transactions):
    token_length-=1
  
  return token_length



def match_transfer_token(token_transfer: Dict[str,Any], accounts_rule: List[str], list_accounts: Dict[str, str]):
  for field in accounts_rule["list_field"]:
    if token_transfer[field].lower() != list_accounts[accounts_rule["index"][field]].lower():
      return False
  return True

def mark_fee_transfer(token_transfers:List[Dict[str,Any]], detail_fee_account:Dict[str,str], fee_account:str):
  if not detail_fee_account and not fee_account:
    return
  
  for token_transfer in token_transfers:
    if detail_fee_account and all(token_transfer.get(k)== v for k, v in detail_fee_account.items()):
      token_transfer["is_fee"] = True
    elif fee_account and (token_transfer.get("to_token_address")== fee_account):
      token_transfer["is_fee"] = True
    else:
      token_transfer["is_fee"] = False
  
  token_transfers.sort(key=lambda x: x.get("is_fee", False))
  
def set_amount(token: dict, wallet_address:str):
  amount = token["amount"]
  if token["from_address"] == wallet_address:
    return -amount
  else:
    return amount
      
def sum_token_transfers(transfers: List[Dict[str, Any]], exclude_fee: bool=True):
  totals = defaultdict(float)
  for tx in transfers:
    if exclude_fee and tx.get("is_fee", False):
      continue

    amount = float(tx.get("amount", 0))
    symbol = tx["symbol"]
    
    totals[symbol] = totals.get(symbol,0) + amount
      
  return {sym: amt for sym, amt in totals.items() if amt != 0}

def process_fee_transfer(instruction, discriminator_key):
  disc_info = discriminators[discriminator_key]
  
  if not disc_info.get("fee"):
    return 0, {}
  
  fee_index = disc_info.get("fee_index", None)
  if fee_index is None:
    return 0, {}
  
  fee_transfer = instruction[fee_index]
  transfer_accounts = fee_transfer["accounts"]
  raw_data_transfer = b58decode(fee_transfer["data"])
  
  if len(raw_data_transfer) == 9:
    amount, detail_account = decode_transfer_token(raw_data_transfer, transfer_accounts)
    return amount, detail_account
  
  return 0, {}


def process_token_transfer(wallet_address:str, token_transfers, accounts_rule, list_accounts,detail_fee_account, fee_account=""):
  results = []
  
  if accounts_rule is not None:
    for token_transfer in token_transfers:
      tx = {
        "from_token_address": token_transfer["fromTokenAccount"],
        "to_token_address": token_transfer["toTokenAccount"],
        "from_address": token_transfer["fromUserAccount"],
        "to_address": token_transfer["toUserAccount"],
        "amount": token_transfer["tokenAmount"],
        "contract": token_transfer["mint"]
      }
      
      for account_rule in accounts_rule:
        if match_transfer_token(tx, account_rule,list_accounts):
          symbol = get_symbol(tx["contract"])
          amount = set_amount(tx, wallet_address)
          tx["symbol"] = symbol
          tx["amount"] = amount
          results.append(tx)
    mark_fee_transfer(results, detail_fee_account, fee_account)
      
  else:
    for token_transfer in token_transfers:
      tx = {
        "from_token_address": token_transfer["fromTokenAccount"],
        "to_token_address": token_transfer["toTokenAccount"],
        "from_address": token_transfer["fromUserAccount"],
        "to_address": token_transfer["toUserAccount"],
        "amount": token_transfer["tokenAmount"],
        "contract": token_transfer["mint"]
      }
      
      amount = set_amount(tx, wallet_address)
      if tx["from_address"].lower() == wallet_address.lower() or tx["to_address"].lower() == wallet_address.lower():
        tx["amount"] = amount
        symbol = get_symbol(tx["contract"])
        tx["symbol"] = symbol
        results.append(tx)
    mark_fee_transfer(results, detail_fee_account, fee_account)

  results = merge_token_transfers(results, wallet_address)
  return results

def process_native_transfer(wallet_address: str, tx_transfers:list ,native_transfers: list, min_sol: float = 0.001) -> list:
  results = []
  LAMPORTS_PER_SOL = 10**9
  
  for n_transfer in native_transfers:
    existing_transfer = False
    for t_transfer in tx_transfers:
      pair = (n_transfer["fromUserAccount"],n_transfer["toUserAccount"])
      if pair in [(t_transfer["to_token_address"],t_transfer["to_address"]),(t_transfer["from_address"],t_transfer["from_token_address"])]:
        existing_transfer = True
        continue

    if not existing_transfer:
      tx = {
        "from_address": n_transfer["fromUserAccount"],
        "to_address": n_transfer["toUserAccount"]
      }

      amount = n_transfer["amount"] / LAMPORTS_PER_SOL
      if amount <= min_sol:
        continue

      if tx["from_address"] == wallet_address:
        amount = -amount
      elif tx["to_address"] != wallet_address:
        continue

      results.append({
        **tx,
        "amount": amount,
        "symbol": "SOL",
        "wallet": tx["from_address"] if tx["to_address"] == wallet_address else tx["to_address"]
      })
      
  return results

def get_raw_transaction(signature: str):
  if not signature:
    return None

  return safe_rpc_post_with_retry("getTransaction", [
    signature,
    {
      "encoding": "json",
      "maxSupportedTransactionVersion": 0
    }
  ])

def get_raw_account_keys(raw_tx: dict) -> list:
  message = raw_tx.get("transaction", {}).get("message", {})
  account_keys = []
  for account in message.get("accountKeys", []):
    if isinstance(account, str):
      account_keys.append(account)
    elif isinstance(account, dict):
      account_keys.append(account.get("pubkey", ""))

  loaded_addresses = raw_tx.get("meta", {}).get("loadedAddresses", {})
  account_keys.extend(loaded_addresses.get("writable", []) or [])
  account_keys.extend(loaded_addresses.get("readonly", []) or [])
  return account_keys

def has_jupiter_program(tx: dict, raw_tx: dict = None) -> bool:
  for instruction in tx.get("instructions", []) or []:
    if instruction.get("programId") in JUPITER_PROGRAM_IDS:
      return True

  if not raw_tx:
    return False

  account_keys = get_raw_account_keys(raw_tx)
  message = raw_tx.get("transaction", {}).get("message", {})
  for instruction in message.get("instructions", []) or []:
    program_index = instruction.get("programIdIndex")
    if isinstance(program_index, int) and program_index < len(account_keys):
      if account_keys[program_index] in JUPITER_PROGRAM_IDS:
        return True
  return False

def token_amount_to_decimal(token_amount: dict) -> Decimal:
  if not token_amount:
    return Decimal(0)

  raw_amount = token_amount.get("amount")
  decimals = token_amount.get("decimals", 0)
  if raw_amount is not None:
    return Decimal(str(raw_amount)) / (Decimal(10) ** int(decimals))

  ui_amount = token_amount.get("uiAmountString")
  if ui_amount is not None:
    return Decimal(str(ui_amount))

  ui_amount = token_amount.get("uiAmount")
  if ui_amount is not None:
    return Decimal(str(ui_amount))

  return Decimal(0)

def extract_token_balance_deltas(raw_tx: dict) -> list:
  meta = raw_tx.get("meta", {})
  account_keys = get_raw_account_keys(raw_tx)
  balances_by_index = {}

  for side, balances in (("pre", meta.get("preTokenBalances", [])), ("post", meta.get("postTokenBalances", []))):
    for balance in balances or []:
      account_index = balance.get("accountIndex")
      if account_index is None:
        continue

      current = balances_by_index.setdefault(account_index, {
        "account_index": account_index,
        "token_account": account_keys[account_index] if account_index < len(account_keys) else "",
        "mint": balance.get("mint", ""),
        "owner": balance.get("owner", ""),
        "decimals": balance.get("uiTokenAmount", {}).get("decimals", 0),
        "pre": Decimal(0),
        "post": Decimal(0)
      })
      current["mint"] = current.get("mint") or balance.get("mint", "")
      current["owner"] = current.get("owner") or balance.get("owner", "")
      current["decimals"] = balance.get("uiTokenAmount", {}).get("decimals", current.get("decimals", 0))
      current[side] = token_amount_to_decimal(balance.get("uiTokenAmount", {}))

  deltas = []
  for balance in balances_by_index.values():
    amount = balance["post"] - balance["pre"]
    if amount == 0:
      continue
    deltas.append({
      **balance,
      "amount": amount
    })
  return deltas

def has_transfer_for_mint(transfers: list, mint: str, negative: bool = None) -> bool:
  mint_lower = mint.lower()
  for transfer in transfers or []:
    if transfer.get("is_fee", False):
      continue
    if transfer.get("contract", "").lower() != mint_lower:
      continue
    amount = Decimal(str(transfer.get("amount", 0)))
    if negative is True and amount >= 0:
      continue
    if negative is False and amount <= 0:
      continue
    return True
  return False

def normalize_decimal_amount(amount: Decimal) -> Decimal:
  if amount == amount.to_integral():
    return amount.quantize(Decimal(1))
  return amount.normalize()

def build_jupiter_swap_context_transfers(wallet_address: str, tx: dict, current_transfers: list) -> list:
  current_transfers = list(current_transfers or [])
  distinct_contracts = {
    transfer.get("contract", "").lower()
    for transfer in current_transfers or []
    if transfer.get("contract") and not transfer.get("is_fee", False)
  }
  if len(distinct_contracts) >= 2:
    return []

  raw_tx = tx.get("raw_transaction")
  if not raw_tx and not has_jupiter_program(tx):
    return []

  if not raw_tx:
    raw_tx = get_raw_transaction(tx.get("signature") or tx.get("hash"))
  if not raw_tx or not has_jupiter_program(tx, raw_tx):
    return []

  deltas = extract_token_balance_deltas(raw_tx)
  if not deltas:
    return []

  wallet_lower = wallet_address.lower()
  wallet_deltas = [delta for delta in deltas if delta.get("owner", "").lower() == wallet_lower]
  output_mints = {delta["mint"] for delta in wallet_deltas if delta["amount"] > 0}
  if not output_mints:
    return []

  wallet_changed_mints = {delta["mint"] for delta in wallet_deltas}
  additions = []
  deltas_by_mint = defaultdict(list)
  for delta in deltas:
    deltas_by_mint[delta["mint"]].append(delta)

  for output_delta in wallet_deltas:
    if output_delta["amount"] <= 0:
      continue
    if has_transfer_for_mint(current_transfers + additions, output_delta["mint"], negative=False):
      continue

    additions.append({
      "from_token_address": "",
      "to_token_address": output_delta.get("token_account", ""),
      "from_address": "",
      "to_address": wallet_address,
      "amount": normalize_decimal_amount(output_delta["amount"]),
      "contract": output_delta["mint"],
      "symbol": get_symbol(output_delta["mint"]),
      "wallet": wallet_address,
      "is_context": False
    })

  input_candidates = []
  for mint, mint_deltas in deltas_by_mint.items():
    if mint in wallet_changed_mints:
      continue
    if has_transfer_for_mint(current_transfers + additions, mint, negative=True):
      continue

    negative_deltas = [delta for delta in mint_deltas if delta["amount"] < 0]
    positive_deltas = [delta for delta in mint_deltas if delta["amount"] > 0]
    if not negative_deltas or not positive_deltas:
      continue

    mint_total = sum((delta["amount"] for delta in mint_deltas), Decimal(0))
    if mint_total != 0:
      continue

    amount = sum((delta["amount"] for delta in negative_deltas), Decimal(0))
    destination = max(positive_deltas, key=lambda delta: abs(delta["amount"])).get("owner", "")
    drained_source = any(delta.get("post") == 0 for delta in negative_deltas)
    input_candidates.append({
      "confidence": 1 if drained_source else 0,
      "abs_amount": abs(amount),
      "transfer": {
        "from_token_address": "",
        "to_token_address": "",
        "from_address": wallet_address,
        "to_address": destination or next(iter(JUPITER_PROGRAM_IDS)),
        "amount": normalize_decimal_amount(amount),
        "contract": mint,
        "symbol": get_symbol(mint),
        "wallet": wallet_address,
        "is_context": True
      }
    })

  if input_candidates:
    input_candidates.sort(key=lambda item: (item["confidence"], item["abs_amount"]), reverse=True)
    additions.append(input_candidates[0]["transfer"])

  if additions:
    logger.info("[SOL] Added %d Jupiter context transfers for tx=%s wallet=%s", len(additions), (tx.get("signature") or tx.get("hash") or "")[:20], wallet_address[:8])
  return additions

def enrich_jupiter_swap_context(wallet_address: str, tx: dict, token_transfers: list) -> list:
  token_transfers = list(token_transfers or [])
  additions = build_jupiter_swap_context_transfers(wallet_address, tx, token_transfers)
  if not additions:
    return token_transfers
  return token_transfers + additions

def initial_transaction_base(tx:dict):
  return {
    "hash": tx["signature"],
    "block": tx["slot"],
    "chain": "SOL",
    "tx_time": convert_block_time(tx["timestamp"]),
    "token_transfers": [],
    "native_transfers": [],
    "transactions": [],
    "nft_account": {},
    "changed_token":{}
  }

def parse_transaction(wallet_address: str, transactions: list, start_time:int, end_time:int):
  list_tx = []
  
  for tx in transactions:
    logger.debug("[SOL] Processing tx=%s", tx["signature"][:20])
    ts = tx["timestamp"]

    transaction = initial_transaction_base(tx)
    # if transaction["hash"] == "iys9nqqMiEZezVTAjgZf476YKkXeYEbLVgt8LgnueCCqMscxLoGAsS5qBHGHMGNpNMmmYoT67ymzzKQK7xJo5Ff":
    #   with open("iys9nqqMiEZezVTAjgZf476YKkXeYEbLVgt8LgnueCCqMscxLoGAsS5qBHGHMGNpNMmmYoT67ymzzKQK7xJo5Ff.json", "w", encoding="utf-8") as f:
    #     json.dump(tx,f, indent=2)

    discriminator=""
    list_accounts = []
    accounts_rule = []
    fee_account = ""
    detail_fee_account = {}
    found_discriminator = False
    token_transfers = tx["tokenTransfers"]
    native_transfers = tx["nativeTransfers"]
    program_id = ""
    
    for instruction in tx["instructions"]:
      if instruction["accounts"] and instruction["innerInstructions"]:
        parsed_discriminator = parse_discriminator(instruction["data"])
        
        if parsed_discriminator not in discriminators:
          continue
        
        if discriminators[parsed_discriminator]["name"] == "close_position":
          get_nft_mint_from_close_position(instruction["accounts"], nfts)
          continue
        
        found_discriminator = True
        discriminator = parsed_discriminator
        list_accounts.extend(instruction["accounts"])
        program_id = instruction["programId"]
        accounts_rule = discriminators[discriminator].get("accounts")
        transaction["type"] = discriminators[discriminator]["name"]
        
        amount = 0
        
        add_fee = discriminators[discriminator].get("fee", False)
        if add_fee == True:
          amount, detail_fee_account = process_fee_transfer(instruction["innerInstructions"], discriminator)
        if discriminators[discriminator].get("fee_account", None) is not None:
          fee_account = list_accounts[discriminators[discriminator]["fee_account"]]
    
    if found_discriminator == True:
      instruction_name = discriminators[discriminator]["name"]
      if instruction_name in ["open_position_with_token22_nft","increase_liquidity_v2","decrease_liquidity_v2"]:
        persional_position = ""
        nft_mint = ""
        nft_account = ""
        if instruction_name == "open_position_with_token22_nft":
          persional_position = list_accounts[8]
          if nfts.get(persional_position, None) is None:
            nft_mint = list_accounts[2]
            nft_account = list_accounts[3]
            
            nfts[persional_position] = {
              "nft_mint": nft_mint,
              "nft_account": nft_account
            }
        elif instruction_name == "increase_liquidity_v2":
          persional_position = list_accounts[4]
          nft_account = list_accounts[1]
        elif instruction_name == "decrease_liquidity_v2":
          persional_position = list_accounts[2]
          nft_account = list_accounts[1]
          
        if persional_position and nfts.get(persional_position, None) is None and nft_mint != "":
          nfts[persional_position] = {
            "token_id": nft_mint,
            "wallet": nft_account
          }
        elif persional_position and nfts.get(persional_position, None) is None and nft_mint == "":
          nft_mint = get_nft_mint_from_persional_position(persional_position)
          nfts[persional_position] = {
            "token_id": nft_mint,
            "wallet": nft_account
          }

        transaction["nft"] = nfts[persional_position]
        transaction["nft"]["contract"] = program_id
      
      tx_transfers = process_token_transfer(wallet_address, token_transfers, accounts_rule, list_accounts, detail_fee_account, fee_account)
      tx_native = process_native_transfer(wallet_address, tx_transfers, native_transfers,0.005)
      tx_transfers = enrich_jupiter_swap_context(wallet_address, tx, tx_transfers)
      transaction["token_transfers"].extend(tx_transfers)
      transaction["native_transfers"].extend(tx_native)
    else:
      tx_transfers = process_token_transfer(wallet_address, token_transfers, None, list_accounts, detail_fee_account, fee_account)
      tx_native = []

      tx_native = process_native_transfer(wallet_address, tx_transfers, native_transfers, 0.005)
      tx_transfers = enrich_jupiter_swap_context(wallet_address, tx, tx_transfers)

      transaction["token_transfers"].extend(tx_transfers)
      transaction["native_transfers"].extend(tx_native)

    transaction["changed_token"] = sum_token_transfers(transaction["token_transfers"]+transaction["native_transfers"])
    transaction["token_transfers"] = remove_intermediate_tokens(transaction["token_transfers"], transaction["changed_token"])
    transaction["transactions"] = transaction["token_transfers"] + transaction["native_transfers"]
    del transaction["token_transfers"]
    del transaction["native_transfers"]
      
    if start_time <= ts < end_time:
      if discriminator or transaction["transactions"]:
        list_tx.append(transaction)
    elif ts < start_time:
      break
  return list_tx


def get_transaction(wallet_address: str, start_time:int, end_time:int, before_signature: str = None, limit: int = 100):
  url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{wallet_address}/transactions"
  
  params = {
    "api-key": HELIUS_API_KEY,
    "token-accounts": "all",
    "limit": limit
  }
  if before_signature:
    params['before'] = before_signature
  
  stop = False
  oldest_signature = ""
  response = safe_fetch_with_retry(url, params)
  if response is not None:
    if not isinstance(response, list):
      logger.error("[SOL] Helius API returned non-list result: %s", response)
      return [], True, oldest_signature
    transactions = response
    if not transactions:
      logger.warning("[SOL] Helius returned empty list for wallet=%s", wallet_address[:8])
      return [], True, oldest_signature

    parse = parse_transaction(wallet_address, transactions, start_time, end_time)
    lasted_ts = transactions[-1]["timestamp"]
    if lasted_ts < start_time:
      stop = True
      oldest_signature = transactions[-1]["signature"]
    else:
      oldest_signature = transactions[-1]["signature"]
    return parse, stop, oldest_signature

  return None, stop, oldest_signature


def fetch_all_transactions(wallet_address: str, start_time: str, end_time: str):
  transactions = []
  before_signature = None
  
  block_time_start = convert_datetime_to_blocktime(start_time) if start_time else 0
  block_time_end = convert_datetime_to_blocktime(end_time, add_time=True) if end_time else 4102444800
  
  
  MAX_PAGES = 200
  page_count = 0
  while True:
    page_count += 1
    if page_count > MAX_PAGES:
      logger.error("[SOL][%s] fetch exceeded MAX_PAGES=%d. Stopping.", wallet_address[:8], MAX_PAGES)
      break

    logger.debug("[SOL][%s] Fetch cursor=%s", wallet_address[:8], before_signature or 'START')
    res_tx, stop, before_signature = get_transaction(wallet_address, block_time_start, block_time_end, before_signature, limit=100)
    
    if res_tx:
      logger.debug("[SOL][%s] Batch: %d txs cursor=%s", wallet_address[:8], len(res_tx), (before_signature or 'START')[:20])
      transactions.extend(res_tx)

    if stop == True:
      break
    
  return transactions

def get_transaction_sol_with_filter(wallet_address: str, start_time:str, end_time:str, symbol:str, mint:str=""):
  transactions = []
  max_length = 0
  res_tx = fetch_all_transactions(wallet_address, start_time, end_time)
  
  if not res_tx:
    return {"transactions":transactions, "max_length":max_length}

  for tx in res_tx:
    transfers = remove_fee_transfer(tx["transactions"])
    searchable_transfers = remove_context_transfers(transfers)
    for transaction in searchable_transfers:
      if mint:
        if transaction.get("symbol","").upper()==symbol.upper() and transaction.get("contract", "").lower()== mint.lower():
          prioritized_tokens = prioritize_token(transfers, symbol, mint)
          tx["details"] = prioritized_tokens
          del tx["transactions"]
          transactions.append(tx)
          token_length = get_length_token_transfers(tx["details"])
          if token_length > max_length:
            max_length = token_length
          break
      else:
        if transaction.get("symbol","").upper() == symbol.upper():
          prioritized_tokens = prioritize_token(transfers, symbol, mint)
          tx["details"] = prioritized_tokens
          del tx["transactions"]
          transactions.append(tx)
          token_length = get_length_token_transfers(tx["details"])
          if token_length > max_length:
            max_length = token_length
          break
  total_transactions = [
    {**tx, "details": remove_context_transfers(tx.get("details", []))}
    for tx in transactions
  ]
  separate_total_symbol = calculate_separate_tokens(total_transactions, symbol)

  return {"transactions":transactions, "max_length":max_length, "total":separate_total_symbol}
