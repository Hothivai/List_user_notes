from web3 import Web3
from eth_account import Account
import math
import math
from decimal import Decimal, getcontext
from services.liquidity_actions.helper import *
from dotenv import load_dotenv
import os
import time
from config import *
from eth_abi import encode

getcontext().prec = 50  # tăng độ chính xác tính toán

load_dotenv()

# PRIVATE_KEY = os.getenv('PRIVATE_KEY')
# ACCOUNT = Account.from_key(PRIVATE_KEY)

def stake_liquidity_position(chain_name, token_id, pid, account, private_key):
    w3 = get_web3_connection(chain_name)
    if not w3.is_connected:
        print(f"❌ Không thể kết nối Web3 tới {chain_name}.")
        return
    
    npm_address = NPM_ADDRESSES.get(chain_name, "unknown")
    if npm_address != "unknown":
        npm_address_cs = Web3.to_checksum_address(npm_address)
    
    npm_abi = get_abi(chain_name, npm_address)
    npm_contract = get_contract(w3, npm_address, npm_abi)

    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown")
    if masterchef_address == "unknown":
        return
    
    masterchef_address_cs = Web3.to_checksum_address(masterchef_address)
    data = encode(['uint256'], [pid])

    stake_tx = npm_contract.functions.safeTransferFrom(account, masterchef_address_cs, token_id, data)
    build_stake_tx = build_transaction_safely(chain_name, stake_tx, account)
    signed_tx = w3.eth.account.sign_transaction(build_stake_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"✅ Staked liquidity position: {tx_hash.hex()}")

def harvest_cake_farm(chain_name, token_id, account, private_key):
    w3 = get_web3_connection(chain_name)
    if not w3.is_connected:
        print(f"❌ Không thể kết nối Web3 tới {chain_name}.")
        return
    
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown")
    if masterchef_address == "unknown":
        return
    masterchef_address_cs = Web3.to_checksum_address(masterchef_address)
    masterchef_abi = get_abi(chain_name, masterchef_address_cs)
    masterchef_contract = get_contract(w3, masterchef_address_cs, masterchef_abi)
    
    user_position_info = masterchef_contract.functions.userPositionInfos(token_id).call()
    pending_reward = masterchef_contract.functions.pendingCake(token_id).call()
    liquidity = user_position_info[0]
    reward = pending_reward / (10**18)
    
    print(f"💰 Liquidity: {liquidity}")
    print(f"💰 Reward: {reward}")
    
    if liquidity > 0 and reward > 0:
        harvest_tx = masterchef_contract.functions.harvest(token_id, account)
        build_harvest_tx = build_transaction_safely(chain_name, harvest_tx, account)
        signed_harvest_tx = w3.eth.account.sign_transaction(build_harvest_tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed_harvest_tx.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"✅ Harvested cake: {tx_hash.hex()}")
    else:
        print("No liquidity or reward to harvest")
    
def unstake_liquidity_position(chain_name, token_id, account, private_key):
    w3 = get_web3_connection(chain_name)
    if not w3.is_connected:
        print(f"❌ Không thể kết nối Web3 tới {chain_name}.")
        return
    
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown")
    if masterchef_address == "unknown":
        return
    masterchef_address_cs = Web3.to_checksum_address(masterchef_address)
    masterchef_abi = get_abi(chain_name, masterchef_address_cs)
    masterchef_contract = get_contract(w3, masterchef_address_cs, masterchef_abi)
    
    unstake_tx = masterchef_contract.functions.withdraw(token_id, account)
    build_unstake_tx = build_transaction_safely(chain_name, unstake_tx, account)
    signed_unstake_tx = w3.eth.account.sign_transaction(build_unstake_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_unstake_tx.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"✅ Unstaked liquidity position: {tx_hash.hex()}")

# if __name__ == "__main__":
    # stake_liquidity_position("BNB", 3439780, 216, ACCOUNT.address, PRIVATE_KEY)
    
    # harvest_cake_farm("BNB", 3455422, ACCOUNT.address, PRIVATE_KEY)
    
    # unstake_liquidity_position("BNB", 3455422, ACCOUNT.address, PRIVATE_KEY)