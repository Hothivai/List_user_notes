import sys
import os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
sys.path.append(PROJECT_ROOT)

from services.excute_transaction import get_lasted_block, get_lasted_signature
from services.excute_transaction_v2 import get_lasted_block_v2, get_lasted_signature_v2
from services.transaction_history.save_sol_tx_his import fetch_all_transactions
from services.transaction_history.save_tx_his import get_new_transactions, CHAIN_ID
from services.transaction_history_v2.save_sol_tx_his_v2 import fetch_all_transactions as fetch_all_transactions_v2
from services.transaction_history_v2.save_tx_his_v2 import get_new_transactions as get_new_transactions_v2
from services.excute_transaction import get_transaction, get_existing_wallet, test_get_transaction

WALLET_ADDRESS_EVM = [
  "0x88de2ab47352779494547caccb31ee1a133dd334",
  "0x349F8F068120E04B359556E442A579Af41ebF486",
  "0x065994BeC6cA97AeF488f76824580814Be4E024F",
  "0x9b73E95909Be63F02b06130716384c3030C74D8D",
  "0x89B8274BbC46A0db474E3Df381688F80DfFccB6b",
  "0x0c9880AEcEDa007fD7967d1672D8C91b85e5c087"
]
WALLET_ADDRESS_SOLANA = [
  "CJoUCt78FNbJJcKW3CnmLG9CVq6ANSTiXWV1tyN5dXw9",
  "4rDyyA4vydw4T5uekxY5La4Ywv43nSZ2PgG7rfBfvQAJ",
  "DGHsf8b99KyWPErCbVuXcPUxAXwaC7bqndPgEVvmSAFn",
  "8x4zj74myKzox48jUMHskfNo4NHuAzXeLyXs7HLUSYzL"
]

if __name__=="__main__":
  # Get transaction of list wallet in EVMS chain
  for wallet in WALLET_ADDRESS_EVM:
    for chain, id in CHAIN_ID.items():
      # lasted_block = get_lasted_block(wallet, chain)
      # print(f"Lasted block in database: {lasted_block}")
      # get_new_transactions(wallet, chain, lasted_block)
      
      lasted_block_v2 = get_lasted_block_v2(wallet, chain)
      print(f"Lasted block in database V2: {lasted_block_v2}")
      get_new_transactions_v2(wallet, chain, lasted_block_v2)
    
  # Get transactions of list wallet of solana chain
  for wallet in WALLET_ADDRESS_SOLANA:
    # lasted_signature = get_lasted_signature(wallet)
    # print(f"lasted signature of wallet {wallet} is {lasted_signature}")
    # fetch_all_transactions(wallet, lasted_signature)
    
    lasted_signature_v2 = get_lasted_signature_v2(wallet)
    print(f"lasted signature of wallet {wallet} in V2 is {lasted_signature_v2}")
    fetch_all_transactions_v2(wallet, lasted_signature_v2)
  
  # transactions = get_transaction("0x88de2ab47352779494547caccb31ee1a133dd334",["BAS", "BSC"],"2025-10-10","2025-10-30","CAKE")
  # print(transactions)
  
  # data = test_get_transaction()
  # print(data)