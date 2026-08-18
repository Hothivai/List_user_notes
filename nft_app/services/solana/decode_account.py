import struct
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from config import *

def parse_token_account(data: bytes):
    # data ở đây chính là acc.account.data từ RPC
    mint = Pubkey(data[0:32])
    owner = Pubkey(data[32:64])
    amount = struct.unpack_from("<Q", data, 64)[0]  # u64 little-endian
    return mint, owner, amount

def decode_pubkey(data: bytes, offset: int):
    return str(Pubkey(data[offset:offset + 32])), offset + 32

def decode_u128(data: bytes, offset: int):
    return int.from_bytes(data[offset:offset + 16], "little"), offset + 16

def decode_i128(data, offset):
    val = int.from_bytes(data[offset:offset+16], "little", signed=True)
    return val, offset + 16

def decode_reward_info_pool(data: bytes, offset: int):
    rewards = []
    for _ in range(3):
        reward_state = struct.unpack_from("<B", data, offset)[0]; offset += 1
        open_time, end_time, last_update_time = struct.unpack_from("<QQQ", data, offset); offset += 24
        emissions_per_second_x64, offset = decode_u128(data, offset)
        reward_total_emissioned, reward_claimed = struct.unpack_from("<QQ", data, offset); offset += 16
        token_mint, offset = decode_pubkey(data, offset)
        token_vault, offset = decode_pubkey(data, offset)
        authority, offset = decode_pubkey(data, offset)
        reward_growth_global_x64, offset = decode_u128(data, offset)

        rewards.append({
            "reward_state": reward_state,
            "open_time": open_time,
            "end_time": end_time,
            "last_update_time": last_update_time,
            "emissions_per_second_x64": emissions_per_second_x64,
            "reward_total_emissioned": reward_total_emissioned,
            "reward_claimed": reward_claimed,
            "token_mint": token_mint,
            "token_vault": token_vault,
            "authority": authority,
            "reward_growth_global_x64": reward_growth_global_x64,
        })
    return rewards, offset

def decode_reward_info_position(data: bytes, offset: int):
    reward_infos = []
    for _ in range(3):  # luôn có 3 reward slot
        growth_inside_last_x64, offset = decode_u128(data, offset)
        reward_amount_owed = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        reward_infos.append({
            "growth_inside_last_x64": growth_inside_last_x64,
            "reward_amount_owed": reward_amount_owed
        })
    return reward_infos, offset

def decode_personal_position_state(client, postion_account):
    position_pubkey = Pubkey.from_string(postion_account)
    resp = client.get_account_info(position_pubkey)
    if not resp or not resp.value:
        raise ValueError("Account info not found")
    
    account_info = resp.value
    data = account_info.data
    
    if len(data) < 281:
        raise ValueError(f"Invalid account data length: {len(data)}")
    
    offset = 8
    bump = struct.unpack_from("<I", data, offset)[0]; offset += 1
    nft_mint = decode_pubkey(data, offset); offset += 32
    pool_id = decode_pubkey(data, offset); offset += 32
    tick_lower_index = struct.unpack_from("<i", data, offset)[0]; offset += 4
    tick_upper_index = struct.unpack_from("<i", data, offset)[0]; offset += 4
    # liquidity = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    # fee_growth_inside_0_last_x64 = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    # fee_growth_inside_1_last_x64 = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    liquidity, offset = decode_u128(data, offset)
    fee_growth_inside_0_last_x64, offset = decode_u128(data, offset)
    fee_growth_inside_1_last_x64, offset = decode_u128(data, offset)
    token_fees_owed_0 = struct.unpack_from("<Q", data, offset)[0]; offset += 8
    token_fees_owed_1 = struct.unpack_from("<Q", data, offset)[0]; offset += 8
    reward_infos, offset = decode_reward_info_position(data, offset)
    
    return {
        "pool_id": pool_id[0],
        "tick_lower_index": tick_lower_index,
        "tick_upper_index": tick_upper_index,
        "liquidity": liquidity,
        "fee_growth_inside_0_last_x64": fee_growth_inside_0_last_x64,
        "fee_growth_inside_1_last_x64": fee_growth_inside_1_last_x64,
        "token_fees_owed_0": token_fees_owed_0,
        "token_fees_owed_1": token_fees_owed_1,
        "reward_infos": reward_infos
    }
    
def decode_personal_position_state_raw(raw_data):
    data = raw_data
    if len(data) < 281:
        raise ValueError(f"Invalid account data length: {len(data)}")
    
    offset = 8
    bump = struct.unpack_from("<I", data, offset)[0]; offset += 1
    nft_mint = decode_pubkey(data, offset); offset += 32
    pool_id = decode_pubkey(data, offset); offset += 32
    tick_lower_index = struct.unpack_from("<i", data, offset)[0]; offset += 4
    tick_upper_index = struct.unpack_from("<i", data, offset)[0]; offset += 4
    # liquidity = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    # fee_growth_inside_0_last_x64 = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    # fee_growth_inside_1_last_x64 = struct.unpack_from("<Q", data, offset)[0]; offset += 16
    liquidity, offset = decode_u128(data, offset)
    fee_growth_inside_0_last_x64, offset = decode_u128(data, offset)
    fee_growth_inside_1_last_x64, offset = decode_u128(data, offset)
    token_fees_owed_0 = struct.unpack_from("<Q", data, offset)[0]; offset += 8
    token_fees_owed_1 = struct.unpack_from("<Q", data, offset)[0]; offset += 8
    reward_infos, offset = decode_reward_info_position(data, offset)
    
    return {
        "pool_id": pool_id[0],
        "tick_lower_index": tick_lower_index,
        "tick_upper_index": tick_upper_index,
        "liquidity": liquidity,
        "fee_growth_inside_0_last_x64": fee_growth_inside_0_last_x64,
        "fee_growth_inside_1_last_x64": fee_growth_inside_1_last_x64,
        "token_fees_owed_0": token_fees_owed_0,
        "token_fees_owed_1": token_fees_owed_1,
        "reward_infos": reward_infos
    }

def decode_pool_state(client, pool_account):
    pool_pubkey = Pubkey.from_string(pool_account)
    resp = client.get_account_info(pool_pubkey)
    
    if not resp or not resp.value:
        raise ValueError("Pool account not found")
    
    account_info = resp.value
    raw_data = account_info.data
    
    """Parse raw data from a pool account."""
    if len(raw_data) < 1544:
        raise ValueError("Invalid data length for pool account")

    offset = 8
    
    # Extracting fields based on the expected layout
    bump = int.from_bytes(raw_data[offset:offset+1], "little"); offset += 1
    
    # Pubkey parsing
    amm_config, offset = decode_pubkey(raw_data, offset)
    owner, offset = decode_pubkey(raw_data, offset)
    token_mint_0, offset = decode_pubkey(raw_data, offset)
    token_mint_1, offset = decode_pubkey(raw_data, offset)
    token_vault_0, offset = decode_pubkey(raw_data, offset)
    token_vault_1, offset = decode_pubkey(raw_data, offset)
    observation_key, offset = decode_pubkey(raw_data, offset)
    
    # Decimals parsing 
    mint_decimals_0 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    mint_decimals_1 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    
    # Pool value parsing
    tick_spacing, = struct.unpack_from("<H", raw_data, offset); offset += 2
    liquidity, offset = decode_u128(raw_data, offset)
    sqrt_price_x64, offset = decode_u128(raw_data, offset)
    
    tick_current, = struct.unpack_from("<i", raw_data, offset); offset += 4
    
    padding_3 = int.from_bytes(raw_data[offset:offset + 2], "little"); offset += 2
    padding_4 = int.from_bytes(raw_data[offset:offset + 2], "little"); offset += 2
    
    fee_growth_global_0_x64, offset = decode_u128(raw_data, offset)
    fee_growth_global_1_x64, offset = decode_u128(raw_data, offset)
    
    protocol_fees_token_0 = int.from_bytes(raw_data[offset:offset+8], "little"); offset += 8
    protocol_fees_token_1 = int.from_bytes(raw_data[offset:offset+8], "little"); offset += 8
    
    swap_in_amount_token_0, offset = decode_u128(raw_data, offset)
    swap_out_amount_token_1, offset = decode_u128(raw_data, offset)
    swap_in_amount_token_1, offset = decode_u128(raw_data, offset)
    swap_out_amount_token_0, offset = decode_u128(raw_data, offset)
    
    status = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    
    offset += 7

    reward_infos, offset = decode_reward_info_pool(raw_data, offset)
    
    return {
        "token_mint_0": str(token_mint_0),
        "token_mint_1": str(token_mint_1),
        "mint_decimals_0": mint_decimals_0,
        "mint_decimals_1": mint_decimals_1,
        "liquidity": liquidity,
        "sqrt_price_x64": sqrt_price_x64,
        "tick_current": tick_current,
        "fee_growth_global_0_x64": fee_growth_global_0_x64,
        "fee_growth_global_1_x64": fee_growth_global_1_x64,
        "tick_spacing": tick_spacing,
        "reward_infos": reward_infos
    }
    
def decode_pool_state_raw(raw_data):
    """Parse raw data from a pool account."""
    if len(raw_data) < 1544:
        raise ValueError("Invalid data length for pool account")

    offset = 8
    
    # Extracting fields based on the expected layout
    bump = int.from_bytes(raw_data[offset:offset+1], "little"); offset += 1
    
    # Pubkey parsing
    amm_config, offset = decode_pubkey(raw_data, offset)
    owner, offset = decode_pubkey(raw_data, offset)
    token_mint_0, offset = decode_pubkey(raw_data, offset)
    token_mint_1, offset = decode_pubkey(raw_data, offset)
    token_vault_0, offset = decode_pubkey(raw_data, offset)
    token_vault_1, offset = decode_pubkey(raw_data, offset)
    observation_key, offset = decode_pubkey(raw_data, offset)
    
    # Decimals parsing 
    mint_decimals_0 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    mint_decimals_1 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    
    # Pool value parsing
    tick_spacing, = struct.unpack_from("<H", raw_data, offset); offset += 2
    liquidity, offset = decode_u128(raw_data, offset)
    sqrt_price_x64, offset = decode_u128(raw_data, offset)
    
    tick_current, = struct.unpack_from("<i", raw_data, offset); offset += 4
    
    padding_3 = int.from_bytes(raw_data[offset:offset + 2], "little"); offset += 2
    padding_4 = int.from_bytes(raw_data[offset:offset + 2], "little"); offset += 2
    
    fee_growth_global_0_x64, offset = decode_u128(raw_data, offset)
    fee_growth_global_1_x64, offset = decode_u128(raw_data, offset)
    
    protocol_fees_token_0 = int.from_bytes(raw_data[offset:offset+8], "little"); offset += 8
    protocol_fees_token_1 = int.from_bytes(raw_data[offset:offset+8], "little"); offset += 8
    
    swap_in_amount_token_0, offset = decode_u128(raw_data, offset)
    swap_out_amount_token_1, offset = decode_u128(raw_data, offset)
    swap_in_amount_token_1, offset = decode_u128(raw_data, offset)
    swap_out_amount_token_0, offset = decode_u128(raw_data, offset)
    
    status = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
    
    offset += 7

    reward_infos, offset = decode_reward_info_pool(raw_data, offset)
    
    return {
        "token_mint_0": str(token_mint_0),
        "token_mint_1": str(token_mint_1),
        "mint_decimals_0": mint_decimals_0,
        "mint_decimals_1": mint_decimals_1,
        "liquidity": liquidity,
        "sqrt_price_x64": sqrt_price_x64,
        "tick_current": tick_current,
        "fee_growth_global_0_x64": fee_growth_global_0_x64,
        "fee_growth_global_1_x64": fee_growth_global_1_x64,
        "tick_spacing": tick_spacing,
        "reward_infos": reward_infos,
        "token_vault_0": str(token_vault_0),
        "token_vault_1": str(token_vault_1),
    }
        
def get_position_account_by_mint(mint_address: str | Pubkey):
    if isinstance(mint_address, Pubkey):
        mint_pubkey = mint_address
    else:
        mint_pubkey = Pubkey.from_string(str(mint_address))
        
    # Derive PDA
    POSITION_PDA, bump = Pubkey.find_program_address(
        [b"position", bytes(mint_pubkey)],
        PANCAKE_PROGRAM_ID
    )
    return POSITION_PDA

def get_metadata_pda(mint: str) -> Pubkey:
    """Get Metadata account PDA for a given mint"""
    mint_key = Pubkey.from_string(mint)
    seeds = [
        b"metadata",
        bytes(METADATA_PROGRAM_ID),
        bytes(mint_key)
    ]
    return Pubkey.find_program_address(seeds, METADATA_PROGRAM_ID)[0]

def decode_metadata_pda(client, mint_address):
    try:
        metadata_pda = get_metadata_pda(mint_address)
        resp = client.get_account_info(metadata_pda)
        if resp is None or resp.value is None:
            print(f"⚠️ Metadata account not found, skipping.")
            return None
        
        data = resp.value.data
        
        if len(data) < 500:
            raise ValueError(f"Invalid account data length: {len(data)}")

        offset = 65

        # Đọc name
        name_len = struct.unpack_from("<I", data, offset)[0]; offset += 4
        name = data[offset:offset + name_len].decode("utf-8"); offset += name_len

        # Symbol
        symbol_len = struct.unpack_from("<I", data, offset)[0]; offset += 4
        symbol = data[offset:offset + symbol_len].decode("utf-8"); offset += symbol_len

        # URI
        uri_len = struct.unpack_from("<I", data, offset)[0]; offset += 4
        uri = data[offset:offset + uri_len].decode("utf-8"); offset += uri_len

        # Seller Fee BPS (u16)
        seller_fee_basis_points = struct.unpack_from("<H", data, offset)[0]

        return {
            "name": name,
            "symbol": symbol
        }
    except Exception as e:
        print(f"❌ Error decoding metadata PDA: {e}")
        return None 
    
def get_token_decimals(client: Client, mint_address: str):
    mint_key = Pubkey.from_string(mint_address)
    mint_info = client.get_account_info(mint_key)

    if mint_info.value is None:
        raise ValueError("Mint account not found")

    raw_data = bytes(mint_info.value.data)

    # Theo layout SPL Token Mint (82 bytes)
    supply = struct.unpack_from("<Q", raw_data, 36)[0]  # total supply
    decimals = raw_data[44]
    
    return decimals
    
def derive_tick_array_pda(pool_pubkey: Pubkey, start_tick_index: int) -> Pubkey:
    seed_index_bytes = start_tick_index.to_bytes(4, "big", signed=True)
    seeds = [
        b"tick_array",
        bytes(pool_pubkey),
        seed_index_bytes
    ]
    pda, _ = Pubkey.find_program_address(seeds, PANCAKE_PROGRAM_ID)
    return pda

def decode_tick_state(data, offset):
    tick, = struct.unpack_from("<i", data, offset)
    offset += 4

    liquidity_net, offset = decode_i128(data, offset)
    liquidity_gross, offset = decode_u128(data, offset)
    fee_growth_outside_0_x64, offset = decode_u128(data, offset)
    fee_growth_outside_1_x64, offset = decode_u128(data, offset)

    reward_growths = []
    for _ in range(3):
        val, offset = decode_u128(data, offset)
        reward_growths.append(val)

    # padding 32 or 52 byte? --> skip để align 160 byte
    offset += 52  

    return {
        "tick": tick,
        "liquidity_net": liquidity_net,
        "liquidity_gross": liquidity_gross,
        "fee_growth_outside_0_x64": fee_growth_outside_0_x64,
        "fee_growth_outside_1_x64": fee_growth_outside_1_x64,
        "reward_growths_outside_x64": reward_growths
    }, offset

def decode_tick_array_state(client, tick_array_pda, tick):
    tick_array_pubkey = Pubkey.from_string(tick_array_pda)
    resp = client.get_account_info(tick_array_pubkey)
    if resp is None or resp.value is None:
        raise ValueError("Tick array account not found")
    data = resp.value.data
    
    if len(data) < 10240:
        raise ValueError(f"Invalid account data length: {len(data)}")
    
    offset = 8
    pool_id, offset = decode_pubkey(data, offset)
    start_tick_index = struct.unpack_from("<i", data, offset)[0] 
    offset += 4
    
    ticks = []
    for _ in range(60):
        tick_state, offset = decode_tick_state(data, offset)
        if tick_state["tick"] == tick:
            ticks.append(tick_state)

    initialized_tick_count = data[offset]
    offset += 1

    recent_epoch, = struct.unpack_from("<Q", data, offset)
    offset += 8
    
    return {
        "pool_id": pool_id,
        "start_tick_index": start_tick_index,
        "initialized_tick_count": initialized_tick_count,
        "recent_epoch": recent_epoch,
        "ticks": ticks
    }