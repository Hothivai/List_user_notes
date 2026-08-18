import json
from datetime import datetime
from web3 import Web3
from services.execute_data import insert_nft_data
from services.solana.get_wallet_info import process_nft_mint_data_sol
from services.list_farm_pancake import process_nft_mint_data_evm, get_abi, get_contract, get_web3
from services.aerodrome_dex.list_positions_aerodrome import process_nft_mint_data_evm_aerodrome
from config import (
    CHAIN_API_MAP,
    NPM_ADDRESSES,
    FACTORY_ADDRESSES,
    MASTERCHEF_ADDRESSES,
    AERODROME_NPM_ADDRESSES,
    AERODROME_NPM_FACTORY_ADDRESSES,
)
from services.update_query import get_total_alloc_point_each_chain, get_total_cake_per_day_on_chain, process_nft_summary
from services.pancake_api import get_aero_price_usd

def serialize_value(v):
    if isinstance(v, datetime):
        return v.isoformat()
    elif isinstance(v, (list, tuple)):
        return [serialize_value(x) for x in v]
    elif isinstance(v, dict):
        return {k: serialize_value(val) for k, val in v.items()}
    return v

def get_aerodrome_factory_for_npm(chain, npm_address):
    try:
        npm_checksum = Web3.to_checksum_address(npm_address)
    except Exception:
        return None

    factory_map = AERODROME_NPM_FACTORY_ADDRESSES.get(chain, {})
    factory_address = factory_map.get(npm_checksum)
    if factory_address:
        return factory_address

    npm_key = npm_checksum.lower()
    for configured_npm, configured_factory in factory_map.items():
        if str(configured_npm).lower() == npm_key:
            return configured_factory
    return None

def process_nft(nft_id, chain, wallet):
    # Lấy dữ liệu NFT trực tiếp
    data = process_nft_mint_data_sol(
        nft_id, chain, wallet,
        status_map={}, position_map={}, pool_map={}, inactived_nft_ids=[]
    )
    
    if data is None:
        # Không có data → báo lỗi
        return False, None

    # Ghi trực tiếp vào DB và lấy ID của bản ghi vừa chèn
    pos_ids = insert_nft_data([data])
    pos_id = pos_ids[0] if pos_ids else None

    # Update backfill nft summary và cập nhật PnL ngược lại snapshot
    process_nft_summary(data, position_id=pos_id)
    
    # Nếu cần serialize dữ liệu để trả về
    serialized_data = serialize_value(data)

    # Trả về dữ liệu luôn, không publish qua Redis
    return True, serialized_data

def process_nft_evm(nft_id, chain, wallet):
    w3 = get_web3(chain)
    chain_api = CHAIN_API_MAP.get(chain)
    # Get multipliers for each chain
    multiplier_chain = get_total_alloc_point_each_chain(chain=chain)

    # Get total cake reward per second on chain
    total_cake_per_day_of_chain = get_total_cake_per_day_on_chain(chain)
    
    # Total cake reward per second each chain
    cake_per_second = total_cake_per_day_of_chain / 86400

    npm_address = NPM_ADDRESSES.get(chain, "unknown")
    factory_address = FACTORY_ADDRESSES.get(chain, "unknown")
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain, "unknown")
    
    npm_abi = get_abi(chain, npm_address)
    factory_abi = get_abi(chain, factory_address)
    masterchef_abi = get_abi(chain, masterchef_address)
    
    npm_contract = get_contract(w3, npm_address, npm_abi)
    factory_contract = get_contract(w3, factory_address, factory_abi)
    masterchef_contract = get_contract(w3, masterchef_address, masterchef_abi)
    
    data = process_nft_mint_data_evm(
        chain, wallet, nft_id,
        status_map={}, position_map={}, factory_contract=factory_contract,
        w3=w3, chain_api=chain_api,multiplier_chain=multiplier_chain,
        cake_per_second=cake_per_second, npm_contract=npm_contract,
        masterchef_contract=masterchef_contract, inactived_nft_ids=[],
        npm_abi=npm_abi, masterchef_abi=masterchef_abi, mode="cron"
    )
    
    if data is None:
        # Không có data → báo lỗi
        return False, None
    
    pos_ids = insert_nft_data([data])
    pos_id = pos_ids[0] if pos_ids else None
    
    # Update backfill nft summary
    process_nft_summary(data, position_id=pos_id)
    
    # Nếu cần serialize dữ liệu để trả về
    serialized_data = serialize_value(data)

    # Trả về dữ liệu luôn, không publish qua Redis
    return True, serialized_data

def process_nft_evm_aerodrome(nft_id, chain, wallet, npm_address=None):
    w3 = get_web3(chain)
    
    # Get Aero Price USD
    aero_price_usd = get_aero_price_usd()
    
    if npm_address:
        npm_addresses = [Web3.to_checksum_address(npm_address)]
    else:
        npm_addresses = AERODROME_NPM_ADDRESSES.get(chain, [])
    matches = []
    
    for npm_address in npm_addresses:
        try:
            factory_address = get_aerodrome_factory_for_npm(chain, npm_address)
            if not factory_address:
                print(f"Missing Aerodrome factory mapping for NPM {npm_address}")
                continue
            npm_abi = get_abi(chain, npm_address)
            
            npm_contract = get_contract(w3, npm_address, npm_abi)
            
            data = process_nft_mint_data_evm_aerodrome(
                chain, wallet, nft_id,
                status_map={}, position_map={},
                w3=w3, npm_address=npm_address, npm_contract=npm_contract, 
                npm_abi=npm_abi, inactived_nft_ids=[],
                aero_price=aero_price_usd, mode="cron",
                factory_address=factory_address,
            )
            
            if data is None:
                continue 
            matches.append((npm_address, factory_address, data))

        except Exception as e:
            print(f"Error checking Aerodrome NPM {npm_address}: {e}")
            continue

    if len(matches) > 1:
        return False, {
            "error": "ambiguous_aerodrome_npm",
            "matches": [
                {"npm_address": npm, "factory_address": factory}
                for npm, factory, _ in matches
            ],
        }

    if not matches:
        return False, None

    _, _, data = matches[0]
    pos_ids = insert_nft_data([data])
    pos_id = pos_ids[0] if pos_ids else None

    # Update backfill nft summary
    process_nft_summary(data, position_id=pos_id)

    serialized_data = serialize_value(data)
    return True, serialized_data
