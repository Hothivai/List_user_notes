import json
import os
import time

import requests
from dotenv import load_dotenv
from decimal import  getcontext

from services.transaction_history.sol_tx_his import HELIUS_API_KEY, SYMBOL_CACHE_PATH, discriminators, get_metadata, parse_discriminator,get_nft_mint_from_close_position, get_nft_mint_from_persional_position, remove_intermediate_tokens, process_token_transfer, process_native_transfer,sum_token_transfers, process_fee_transfer, initial_transaction_base
from services.excute_transaction import insert_detail_token_transfer, insert_nft_token, insert_transactions

# ==================== CONFIG ==================== #
load_dotenv(verbose=True)
getcontext().prec = 50


# ==================== SAFE FETCH DATA ==================== #
def safe_fetch_with_retry(url, params = None, max_retries=5, delay=1):
  for attempt in range(max_retries):
    try:
      response = requests.get(url, params=params, timeout=(5,30))
      data = response.json()
      return data
    except requests.RequestException as e:
      print(f"Retry {attempt+1}/{max_retries} after exception: {e}")
      time.sleep(delay)
  
  return None

# ==================== SYMBOL CACHE ==================== #
nfts = {}
def load_symbol_cache(path:str):
  if os.path.exists(path):
    with open(path,"r", encoding="utf-8") as f:
      return json.load(f)
  return {}

def get_symbol(mint_address:str):
  if mint_address in symbol_cache:
    return symbol_cache[mint_address]

  meta_data = get_metadata(mint_address)
  symbol = meta_data["symbol"]
  symbol_cache[mint_address] = symbol
  return symbol

def save_symbol_cache(symbol_cache):
  with open(SYMBOL_CACHE_PATH,"w", encoding="utf-8") as f:
    json.dump(symbol_cache, f, ensure_ascii=False, indent=2)

symbol_cache = load_symbol_cache(SYMBOL_CACHE_PATH)


# ==================== HELPER FUNCTIONS AND METADATA ==================== #



def parse_transaction(wallet_address: str, transactions: list):
  list_tx = []
  
  for tx in transactions:
    # print(f"Signature of tx {tx["signature"]}")
    # ts = tx["timestamp"]

    transaction = initial_transaction_base(tx)
    
    discriminator=""
    list_accounts = []
    accounts_rule = []
    fee_account = ""
    detail_fee_account = {}
    found_discriminator = False
    token_transfers = tx["tokenTransfers"]
    native_transfers = tx["nativeTransfers"]
    program_id = ""
    # nfts = []
    
    for instruction in tx["instructions"]:
      if instruction["accounts"] and instruction["innerInstructions"]:
        parsed_discriminator = parse_discriminator(instruction["data"])
        
        if parsed_discriminator not in discriminators:
          continue
        
        if discriminators[parsed_discriminator]["name"] == "close_position":
          get_nft_mint_from_close_position(instruction["accounts"], nfts)
          continue
        
        found_discriminator = True
        # if parsed_discriminator in discriminators:
        # print(f"Signature {tx["signature"]} has type {discriminators[parsed_discriminator]["name"]}")
        discriminator = parsed_discriminator
        list_accounts.extend(instruction["accounts"])
        program_id = instruction["programId"]
        accounts_rule = discriminators[discriminator]["accounts"]
        transaction["type"] = discriminators[discriminator]["name"]
        
        amount = 0
        # detail_account = {}
        
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
              "token_id": nft_mint,
              "wallet": nft_account
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

        transaction["nft_accounts"] = nfts[persional_position]
        transaction["nft_accounts"]["contract"] = program_id

      
      tx_transfers = process_token_transfer(wallet_address, token_transfers, accounts_rule, list_accounts, detail_fee_account, fee_account)
      tx_native = process_native_transfer(wallet_address, tx_transfers, native_transfers,0.003)
      transaction["token_transfers"].extend(tx_transfers)
      transaction["native_transfers"].extend(tx_native)
    else:
      # print(f"Signature {tx["signature"]} has not discriminator")
      tx_transfers = process_token_transfer(wallet_address, token_transfers, None, list_accounts, detail_fee_account, fee_account)
      tx_native = []

      tx_native = process_native_transfer(wallet_address, tx_transfers ,native_transfers,0.003)
      transaction["token_transfers"].extend(tx_transfers)
      transaction["native_transfers"].extend(tx_native)
            
    transaction["changed_token"] = sum_token_transfers(transaction["token_transfers"]+transaction["native_transfers"])
    transaction["token_transfers"] = remove_intermediate_tokens(transaction["token_transfers"], transaction["changed_token"])
    transaction["transactions"] = transaction["token_transfers"] + transaction["native_transfers"]
    del transaction["token_transfers"]
    del transaction["native_transfers"]
      
    if discriminator or transaction["transactions"]:
      list_tx.append(transaction)

  return list_tx


def get_transaction(wallet_address: str, before_signature: str = None, limit: int = 100):
  url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/transactions"
  
  params = {
    "api-key": HELIUS_API_KEY,
    "limit": limit,
  }
  if before_signature:
    params['before'] = before_signature
  
  response = safe_fetch_with_retry(url, params)
 
  if response is not None:
    transactions = response
    if not transactions:
      return [], None
    
    parse = parse_transaction(wallet_address, transactions)

    signature = transactions[-1]["signature"]
    return parse, signature

  return [], ""

def fetch_all_transactions(wallet_address: str, lasted_signature: str = None):
  transactions = []
  before_signature = None

  while True:
    print(f"Fetching transaction with before signature: {before_signature}")
    res_tx, before_signature = get_transaction(wallet_address, before_signature, limit=100)

    print(f"get lasted signature: {before_signature}")
    
    if not res_tx:
      print("No more transactions found or error occurred.")
      break
    # if signature is existing in database, stop fetch
    if lasted_signature and any(tx["hash"]==lasted_signature for tx in res_tx):
      index = next(i for i, tx in enumerate(res_tx) if tx["hash"]==lasted_signature)
      new_txs = res_tx[:index]
      transactions.extend(new_txs)
      print("Reached the lasted signature in database. Stopping fetch.")
      break
    # else, add all transactions
    transactions.extend(res_tx)
      
  # Extract details and nft transactions
  normal_transactions = []
  detail_transfers = []
  nft_tokens = []

  for tx in transactions:
    normal_transactions.append({
      "hash": tx["hash"],
      "block": str(tx["block"]),
      "tx_time": tx["tx_time"],
      "wallet": wallet_address,
      "chain": tx["chain"]
    })
    
    if tx["transactions"]:
      
      for detail in tx["transactions"]:
        if not detail.get("is_fee", False):
          detail_transfers.append({
            "hash": tx["hash"],
            "from_address": detail.get("from_address",""),
            "to_address": detail.get("to_address",""),
            "contract": detail.get("contract",""),
            "amount": str(detail.get("amount",0)),
            "symbol": detail.get("symbol",""),
            "wallet": detail.get("wallet",""),
          })
  
    if tx.get("nft_accounts"):
      nft_tokens.append({
        "hash": tx["hash"],
        "contract": tx["nft_accounts"]["contract"],
        "token_id": tx["nft_accounts"]["token_id"],
        "wallet": tx["nft_accounts"]["wallet"]
      })
  
  save_symbol_cache(symbol_cache)    
  if normal_transactions:
    insert_transactions(wallet_address, "SOL", normal_transactions)
    insert_detail_token_transfer(detail_transfers)
    insert_nft_token(nft_tokens)
