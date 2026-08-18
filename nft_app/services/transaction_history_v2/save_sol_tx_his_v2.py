import json
import os
import time

import requests
from dotenv import load_dotenv
from decimal import  getcontext

from services.transaction_history_v2.sol_tx_his_v2 import HELIUS_API_KEY, discriminators, get_metadata, parse_discriminator,get_nft_mint_from_close_position, get_nft_mint_from_persional_position, remove_intermediate_tokens, process_token_transfer, process_native_transfer,sum_token_transfers, process_fee_transfer, initial_transaction_base, enrich_jupiter_swap_context
from services.excute_transaction_v2 import insert_detail_token_transfer_v2, insert_transactions_v2, get_symbol_cache_from_db, accumulate_transfers
from logging_setup import tx_history_logger as logger

# ==================== CONFIG ==================== #
load_dotenv(verbose=True)
getcontext().prec = 50


# ==================== SAFE FETCH DATA ==================== #
def safe_fetch_with_retry(url, params = None, max_retries=5, delay=1):
  for attempt in range(max_retries):
    try:
      response = requests.get(url, params=params, timeout=(5,30))
      if response.status_code != 200:
        logger.warning("[SOL-SAVE] Helius API Error: status=%d, response=%s", response.status_code, response.text)
        time.sleep(delay)
        continue
      data = response.json()
      return data
    except requests.RequestException as e:
      logger.warning("[SOL-SAVE] API retry %d/%d after exception: %s", attempt + 1, max_retries, e)
      time.sleep(delay)
  
  return None

# ==================== SYMBOL CACHE ==================== #
nfts = {}

symbol_cache = get_symbol_cache_from_db()

def get_symbol(mint_address:str):
  if mint_address in symbol_cache:
    return symbol_cache[mint_address]

  meta_data = get_metadata(mint_address)
  symbol = meta_data["symbol"]
  symbol_cache[mint_address] = symbol
  return symbol


# ==================== HELPER FUNCTIONS AND METADATA ==================== #



def parse_transaction(wallet_address: str, transactions: list):
  list_tx = []
  
  for tx in transactions:
    logger.debug("[SOL-SAVE] Processing tx=%s", tx["signature"][:20])
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
    # Track NFT-specific data separately
    nft_instruction_accounts = None
    nft_instruction_name = ""
    nft_program_id = ""
    
    NFT_INSTRUCTIONS = {"open_position_with_token22_nft", "increase_liquidity_v2", "decrease_liquidity_v2", "close_position"}
    
    for instruction in tx["instructions"]:
      if instruction["accounts"] and instruction.get("data"):
        parsed_discriminator = parse_discriminator(instruction["data"])
        
        if parsed_discriminator not in discriminators:
          continue
        
        instr_name = discriminators[parsed_discriminator]["name"]
        
        # Handle NFT-related instructions: save accounts SEPARATELY
        if instr_name in NFT_INSTRUCTIONS:
          # Cache NFT data for close_position
          if instr_name == "close_position":
            get_nft_mint_from_close_position(instruction["accounts"], nfts)
          
          # Store NFT instruction's own accounts (not mixed with swap accounts)
          nft_instruction_accounts = instruction["accounts"]
          nft_instruction_name = instr_name
          nft_program_id = instruction["programId"]
          
          found_discriminator = True
          discriminator = parsed_discriminator
          # Still extend list_accounts for token transfer processing
          list_accounts.extend(instruction["accounts"])
          program_id = instruction["programId"]
          accounts_rule = discriminators[discriminator].get("accounts")
          transaction["type"] = instr_name
        else:
          # Non-NFT instructions (swap, route, fill, etc.)
          found_discriminator = True
          discriminator = parsed_discriminator
          list_accounts.extend(instruction["accounts"])
          program_id = instruction["programId"]
          accounts_rule = discriminators[discriminator].get("accounts")
          transaction["type"] = instr_name
        
        amount = 0
        
        add_fee = discriminators[parsed_discriminator].get("fee", False)
        if add_fee == True and instruction.get("innerInstructions"):
          amount, detail_fee_account = process_fee_transfer(instruction["innerInstructions"], parsed_discriminator)
        if discriminators[parsed_discriminator].get("fee_account", None) is not None:
          fee_account = list_accounts[discriminators[parsed_discriminator]["fee_account"]]
    
    if found_discriminator == True:
      
      # NFT handling: only if we found an NFT-related instruction
      if nft_instruction_accounts and nft_instruction_name in NFT_INSTRUCTIONS:
        persional_position = ""
        nft_mint = ""
        nft_account = ""
        # Use nft_instruction_accounts (clean, not mixed with swap accounts)
        if nft_instruction_name == "open_position_with_token22_nft":
          persional_position = nft_instruction_accounts[8]
          if nfts.get(persional_position, None) is None:
            nft_mint = nft_instruction_accounts[2]
            nft_account = nft_instruction_accounts[3]
            
            nfts[persional_position] = {
              "token_id": nft_mint,
              "wallet": nft_account
            }
        elif nft_instruction_name == "increase_liquidity_v2":
          persional_position = nft_instruction_accounts[4]
          nft_account = nft_instruction_accounts[1]
        elif nft_instruction_name == "decrease_liquidity_v2":
          persional_position = nft_instruction_accounts[2]
          nft_account = nft_instruction_accounts[1]
        elif nft_instruction_name == "close_position":
          persional_position = nft_instruction_accounts[3]
          nft_account = nft_instruction_accounts[2]
          
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
        transaction["nft_accounts"]["contract"] = nft_program_id

      
      tx_transfers = process_token_transfer(wallet_address, token_transfers, accounts_rule, list_accounts, detail_fee_account, fee_account)
      tx_native = process_native_transfer(wallet_address, tx_transfers, native_transfers,0.003)
      tx_transfers = enrich_jupiter_swap_context(wallet_address, tx, tx_transfers)
      transaction["token_transfers"].extend(tx_transfers)
      transaction["native_transfers"].extend(tx_native)
    else:
      logger.debug("[SOL-SAVE] No discriminator for tx=%s", tx["signature"][:20])
      tx_transfers = process_token_transfer(wallet_address, token_transfers, None, list_accounts, detail_fee_account, fee_account)
      tx_native = []

      tx_native = process_native_transfer(wallet_address, tx_transfers ,native_transfers,0.003)
      tx_transfers = enrich_jupiter_swap_context(wallet_address, tx, tx_transfers)
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
  url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{wallet_address}/transactions"
  
  params = {
    "api-key": HELIUS_API_KEY,
    "limit": limit,
    "token-accounts": "all"
  }
  if before_signature:
    params['before'] = before_signature
  
  response = safe_fetch_with_retry(url, params)
 
  if response is not None:
    if not isinstance(response, list):
      logger.error("[SOL-SAVE] Helius API returned non-list result: %s", response)
      return [], "", []
    transactions = response
    if not transactions:
      return [], None, []
    
    parse = parse_transaction(wallet_address, transactions)

    signature = transactions[-1]["signature"]
    return parse, signature, transactions

  return [], "", []

def fetch_all_transactions(wallet_address: str, lasted_signature: str = None):
  transactions = []
  before_signature = None
  MAX_PAGES = 500
  page_count = 0

  while True:
    page_count += 1
    if page_count > MAX_PAGES:
      logger.error("[SOL-SAVE][%s] Fetch exceeded MAX_PAGES=%d. Stopping.", wallet_address[:8], MAX_PAGES)
      break

    logger.debug("[SOL-SAVE][%s] Fetch cursor=%s", wallet_address[:8], before_signature or 'START')
    res_tx, before_signature, raw_transactions = get_transaction(wallet_address, before_signature, limit=100)

    logger.debug("[SOL-SAVE][%s] Cursor advanced: %s raw=%d parsed=%d", wallet_address[:8], (before_signature or '')[:20], len(raw_transactions), len(res_tx))
    
    if not raw_transactions:
      logger.info("[SOL-SAVE][%s] Fetch done. Collected: %d txs", wallet_address[:8], len(transactions))
      break
    # if signature is existing in database, stop fetch
    if lasted_signature and any(tx["signature"]==lasted_signature for tx in raw_transactions):
      raw_index = next(i for i, tx in enumerate(raw_transactions) if tx["signature"]==lasted_signature)
      known_raw_signatures = {tx["signature"] for tx in raw_transactions[raw_index:]}
      new_txs = [tx for tx in res_tx if tx["hash"] not in known_raw_signatures]
      transactions.extend(new_txs)
      logger.info("[SOL-SAVE][%s] Stopped at known signature. New txs: %d", wallet_address[:8], len(new_txs))
      break
    # else, add all transactions
    transactions.extend(res_tx)
      
  # Extract details and format main transactions
  normal_transactions = []
  detail_transfers = []
  seen_details = set()

  for tx in transactions:
    # Build core transaction structure mapped to table transaction_history_v2
    # sol_tx_his_v2 doesn't naturally provide gas fee easily without extra parsing, set default 0 for sol for now
    nft_contract = ""
    nft_token_id = ""
    if tx.get("nft_accounts"):
      nft_contract = tx["nft_accounts"]["contract"]
      nft_token_id = tx["nft_accounts"]["token_id"]

    normal_transactions.append({
      "hash": tx["hash"],
      "block": str(tx["block"]),
      "tx_time": tx["tx_time"],
      "wallet": wallet_address,
      "chain": tx["chain"],
      "contract": nft_contract,
      "token_id": nft_token_id,
      "transaction_fee": 0,
      "gas_fee": 0
    })
    
    if tx.get("transactions"):
      for detail in tx["transactions"]:
        if not detail.get("is_fee", False):
          # Deduplicate: skip if we've already seen this exact detail
          detail_key = (
            tx["hash"],
            detail.get("from_address",""),
            detail.get("to_address",""),
            detail.get("contract",""),
            str(detail.get("amount",0)),
            detail.get("symbol",""),
            bool(detail.get("is_context", False)),
          )
          if detail_key in seen_details:
            continue
          seen_details.add(detail_key)
          
          detail_transfers.append({
            "hash": tx["hash"],
            "from_address": detail.get("from_address",""),
            "to_address": detail.get("to_address",""),
            "contract": detail.get("contract",""),
            "amount": str(detail.get("amount",0)),
            "symbol": detail.get("symbol",""),
            "wallet": detail.get("wallet",""),
            "is_context": bool(detail.get("is_context", False)),
          })
  

  if normal_transactions:
    insert_transactions_v2(wallet_address, "SOL", normal_transactions)
    # Accumulate (Plan B)
    logger.info("[SOL-SAVE][%s] Before accumulation: %d details", wallet_address[:8], len(detail_transfers))
    detail_transfers = accumulate_transfers(detail_transfers)
    logger.info("[SOL-SAVE][%s] After accumulation: %d details", wallet_address[:8], len(detail_transfers))
    
    insert_detail_token_transfer_v2(detail_transfers)
