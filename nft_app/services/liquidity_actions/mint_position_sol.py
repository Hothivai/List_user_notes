
from decimal import Decimal, getcontext
from services.liquidity_actions.helper import *
from dotenv import load_dotenv
from config import *
from services.liquidity_actions.stake_liquidity import *
from services.solana.decode_account import *
from solders.pubkey import Pubkey
from solana.rpc.api import Client
import struct
import math
from solders.keypair import Keypair
from spl.token.instructions import get_associated_token_address, create_associated_token_account, initialize_mint, initialize_account, transfer_checked, InitializeAccountParams, InitializeMintParams, TransferCheckedParams, close_account, CloseAccountParams, transfer, TransferParams
from spl.token.constants import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID, WRAPPED_SOL_MINT
from solders.instruction import AccountMeta, Instruction
from solders.transaction import VersionedTransaction, Transaction
from solders.message import MessageV0
from solders.keypair import Keypair
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.system_program import create_account, CreateAccountParams, CreateAccountWithSeedParams, create_account_with_seed
from solders.sysvar import RENT
from solders.rpc.responses import GetAccountInfoResp
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from base64 import b64encode
import json
import os
import time

print(F"Token Program ID: {TOKEN_PROGRAM_ID}, Associated Token Program ID: {ASSOCIATED_TOKEN_PROGRAM_ID}, System Program ID: {SYSTEM_PROGRAM_ID}, Rent: {RENT}")

TOKEN_PROGRAM_2022_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")

getcontext().prec = 50  # tăng độ chính xác tính toán

load_dotenv()

new_keypair = Keypair()

def get_data_mint_sol(chain, client, pool_account):    
    pool_info_decode = decode_pool_state(client, pool_account)
    
    tick_current = pool_info_decode["tick_current"]
    tick_spacing = pool_info_decode["tick_spacing"]
    sqrtPriceX64 = pool_info_decode["sqrt_price_x64"]
    
    # Lấy thông tin pool
    pool_info = get_pool_sol_info_from_db(chain, pool_account)
    fee_tier_raw = pool_info["fee"]
    token0_address = pool_info["token0_mint"]
    token1_address = pool_info["token1_mint"]
    
    token0_symbol = pool_info["token0_symbol"]
    token1_symbol = pool_info["token1_symbol"]
    token0_decimals = pool_info["token0_decimals"]
    token1_decimals = pool_info["token1_decimals"]
    
    scale_factor = 10 ** token0_decimals / 10 ** token1_decimals
    
    fee_tier = fee_tier_raw
    
    # Giá hiện tại token1 per token0
    price_current = (sqrtPriceX64 / (2**64)) ** 2
    current_price = price_current * scale_factor
    
    return {
        "token0_address": token0_address,
        "token1_address": token1_address,
        "token0_symbol": token0_symbol,
        "token1_symbol": token1_symbol,
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "fee_tier": fee_tier,
        "current_price": current_price,
        "tick_spacing": tick_spacing,
        "scale_factor": scale_factor,
        "sqrtPriceX96": sqrtPriceX64,
    }
    
# =================== HELPER ===================
def price_to_tick(price, dec0, dec1):
    tick = math.log(price / (10 ** (dec0 - dec1)), 1.0001)
    return round(tick)

def align_to_spacing(tick, spacing):
    remainder = tick % spacing
    return tick - remainder

def get_tick_from_price_range(min_price, max_price, dec0, dec1, tick_spacing):
    tick_lower = price_to_tick(min_price, dec0, dec1)
    tick_upper = price_to_tick(max_price, dec0, dec1)
    tick_lower = align_to_spacing(tick_lower, tick_spacing)
    tick_upper = align_to_spacing(tick_upper, tick_spacing)
    return tick_lower, tick_upper

TICK_ARRAY_SIZE = 60

def get_start_tick_index(tick_index: int, tick_spacing: int) -> int:
    # tick_index phải thuộc về một TickArray range
    return (tick_index // (TICK_ARRAY_SIZE * tick_spacing)) * (TICK_ARRAY_SIZE * tick_spacing)

def derive_tick_array_pda(pool_pubkey: Pubkey, start_tick_index: int, program_id: Pubkey) -> Pubkey:
    seed_index_bytes = start_tick_index.to_bytes(4, "big", signed=True)
    seeds = [
        b"tick_array",
        bytes(pool_pubkey),
        seed_index_bytes
    ]
    pda, _ = Pubkey.find_program_address(seeds, program_id)
    return pda

def derive_protocol_position(pool_pubkey: Pubkey, tick_lower: int, tick_upper: int, program_id: Pubkey) -> Pubkey:
    tick_lower = tick_lower.to_bytes(4, "big", signed=True)
    tick_upper = tick_upper.to_bytes(4, "big", signed=True)
    seeds = [
        b"position",                        
        bytes(pool_pubkey),                 
        tick_lower,                         
        tick_upper
    ]
    pda, bump = Pubkey.find_program_address(seeds, program_id)
    return pda, bump

# =================== MAIN FUNCTION ===================
def get_mint_params_sol(
    chain: str,
    client: Client,
    program_id: Pubkey,
    pool_state_pubkey: str,
    min_price: float,
    max_price: float,
    payer_pubkey: str
):
    """
    Chuẩn bị tham số mint position cho instruction open_position_with_token22_nft
    """
    program_pubkey = program_id if isinstance(program_id, Pubkey) else Pubkey.from_string(program_id)
    pool_pubkey = pool_state_pubkey if isinstance(pool_state_pubkey, Pubkey) else Pubkey.from_string(pool_state_pubkey)
    payer_pubkey = payer_pubkey if isinstance(payer_pubkey, Pubkey) else Pubkey.from_string(payer_pubkey)

    # 1️⃣ Fetch pool_state raw data
    # 2️⃣ Fetch pool_state
    pool_state_data: GetAccountInfoResp = client.get_account_info(pool_pubkey)
    if not pool_state_data or not pool_state_data.value:
        raise Exception("❌ Pool state account not found!")

    data = pool_state_data.value.data
    result = decode_pool_state_raw(data)

    token0_mint = result["token_mint_0"]
    token1_mint = result["token_mint_1"]
    token0_decimals = result["mint_decimals_0"]
    token1_decimals = result["mint_decimals_1"]
    tick_spacing = result["tick_spacing"]
    current_tick = result["tick_current"]
    token_vault_0 = result["token_vault_0"]
    token_vault_1 = result["token_vault_1"]
    
    # 3️⃣ Tính tick index và tick array
    tick_lower, tick_upper = get_tick_from_price_range(
        min_price, max_price, token0_decimals, token1_decimals, tick_spacing
    )
    print(f"Tick lower index: {tick_lower}, Tick upper index: {tick_upper}")
    
    tick_array_lower_start = get_start_tick_index(tick_lower, tick_spacing)
    tick_array_upper_start = get_start_tick_index(tick_upper, tick_spacing)
    print(f"pool_pubkey: {pool_pubkey}, program pubkey: {program_pubkey}")
    
    # 4️⃣ Derive PDA (theo IDL)
    protocol_position, bump = derive_protocol_position(pool_pubkey, tick_lower, tick_upper, program_pubkey)

    tick_array_lower = derive_tick_array_pda(pool_pubkey, tick_array_lower_start, program_pubkey)
    tick_array_upper = derive_tick_array_pda(pool_pubkey, tick_array_upper_start, program_pubkey)

    position_nft_mint = Keypair()
    position_nft_account = get_associated_token_address(
        owner=payer_pubkey,
        mint=position_nft_mint.pubkey()
    )

    personal_position, _ = Pubkey.find_program_address(
        [
            b"position",
            bytes(position_nft_mint.pubkey()),
        ],
        program_pubkey,
    )
    
    token0_mint_pubkey = token0_mint if isinstance(token0_mint, Pubkey) else Pubkey.from_string(token0_mint)
    token1_mint_pubkey = token1_mint if isinstance(token1_mint, Pubkey) else Pubkey.from_string(token1_mint)
    
    token_account_0 = get_associated_token_address(
        owner=payer_pubkey,
        mint=token0_mint_pubkey
    )
    token_account_1 = get_associated_token_address(
        owner=payer_pubkey,
        mint=token1_mint_pubkey
    )

    # ✅ 5️⃣ Tổng hợp dữ liệu
    return {
        "token0_mint": str(token0_mint),
        "token1_mint": str(token1_mint),
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "tick_spacing": tick_spacing,
        "current_tick": current_tick,
        "tick_lower_index": tick_lower,
        "tick_upper_index": tick_upper,
        "tick_array_lower_start_index": tick_array_lower_start,
        "tick_array_upper_start_index": tick_array_upper_start,
        "protocol_position": str(protocol_position),
        "tick_array_lower": str(tick_array_lower),
        "tick_array_upper": str(tick_array_upper),
        "personal_position": str(personal_position),
        "position_nft_mint": str(position_nft_mint.pubkey()),
        "position_nft_account": str(position_nft_account),
        "token_account_0": str(token_account_0),
        "token_account_1": str(token_account_1),
        "token_vault_0": str(token_vault_0),
        "token_vault_1": str(token_vault_1),
    }

def build_mint_position_tx_sol(
    client,
    program_id: str,
    payer_pubkey: str,
    position_nft_owner: str,
    pool_state: str,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = False,
    base_flag: bool | None = None
):
    """
    ✅ Build transaction backend cho instruction open_position_with_token22_nft
    """
    program_pubkey = program_id if isinstance(program_id, Pubkey) else Pubkey.from_string(program_id)
    payer_pubkey = payer_pubkey if isinstance(payer_pubkey, Pubkey) else Pubkey.from_string(payer_pubkey)
    position_nft_owner = position_nft_owner if isinstance(position_nft_owner, Pubkey) else Pubkey.from_string(position_nft_owner)
    pool_state = pool_state if isinstance(pool_state, Pubkey) else Pubkey.from_string(pool_state)

    # 1️⃣ Chuẩn bị PDAs & accounts từ params
    accounts = {
        "payer": payer_pubkey,
        "position_nft_owner": position_nft_owner,
        "position_nft_mint": Pubkey.from_string(params["position_nft_mint"]),
        "position_nft_account": Pubkey.from_string(params["position_nft_account"]),
        "pool_state": pool_state,
        "protocol_position": Pubkey.from_string(params["protocol_position"]),
        "tick_array_lower": Pubkey.from_string(params["tick_array_lower"]),
        "tick_array_upper": Pubkey.from_string(params["tick_array_upper"]),
        "personal_position": Pubkey.from_string(params["personal_position"]),
        "token_account_0": Pubkey.from_string(params["token_account_0"]),
        "token_account_1": Pubkey.from_string(params["token_account_1"]),
        "token_vault_0": Pubkey.from_string(params["token_vault_0"]),
        "token_vault_1": Pubkey.from_string(params["token_vault_1"]),
        "vault_0_mint": Pubkey.from_string(params["token0_mint"]),
        "vault_1_mint": Pubkey.from_string(params["token1_mint"]),
    }

    # 2️⃣ Encode args (theo IDL layout)
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)
    data += struct.pack("<i", params["tick_lower_index"])
    data += struct.pack("<i", params["tick_upper_index"])
    data += struct.pack("<i", params["tick_array_lower_start_index"])
    data += struct.pack("<i", params["tick_array_upper_start_index"])
    data += struct.pack("<Q", liquidity & ((1 << 64) - 1)) + struct.pack("<Q", liquidity >> 64)  # u128 little endian
    data += struct.pack("<Q", amount_0_max)
    data += struct.pack("<Q", amount_1_max)
    data += struct.pack("<?", with_metadata)
    if base_flag is None:
        data += struct.pack("<?", False)  # Option<bool> none
    else:
        data += struct.pack("<?", True)   # has value
        data += struct.pack("<?", base_flag)

    # 3️⃣ Build instruction
    keys = [
        AccountMeta(accounts["payer"], True, True),
        AccountMeta(accounts["position_nft_owner"], False, False),
        AccountMeta(accounts["position_nft_mint"], True, True),
        AccountMeta(accounts["position_nft_account"], False, True),
        AccountMeta(accounts["pool_state"], False, True),
        AccountMeta(accounts["protocol_position"], False, True),
        AccountMeta(accounts["tick_array_lower"], False, True),
        AccountMeta(accounts["tick_array_upper"], False, True),
        AccountMeta(accounts["personal_position"], False, True),
        AccountMeta(accounts["token_account_0"], False, True),
        AccountMeta(accounts["token_account_1"], False, True),
        AccountMeta(accounts["token_vault_0"], False, True),
        AccountMeta(accounts["token_vault_1"], False, True),
        AccountMeta(RENT, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_PROGRAM_ID, False, False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_PROGRAM_2022_ID, False, False),
        AccountMeta(accounts["vault_0_mint"], False, False),
        AccountMeta(accounts["vault_1_mint"], False, False),
    ]
    
    ata_instructions = []
    # Kiểm tra token_account_0
    if not client.get_account_info(accounts["token_account_0"]).value:
        ata_instructions.append(
            create_associated_token_account(
                payer=accounts["payer"],
                owner=accounts["payer"],
                mint=Pubkey.from_string(params["token0_mint"])
            )
        )

    # Kiểm tra token_account_1
    if not client.get_account_info(accounts["token_account_1"]).value:
        ata_instructions.append(
            create_associated_token_account(
                payer=accounts["payer"],
                owner=accounts["payer"],
                mint=Pubkey.from_string(params["token1_mint"])
            )
        )

    ix = Instruction(program_pubkey, bytes(data), keys)

    # 4️⃣ Gộp ATA instruction + instruction mint
    tx_instructions = ata_instructions + [ix]

    # 4️⃣ Build transaction (Version 0)
    blockhash = client.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer_pubkey,
        tx_instructions,  # ✅ dùng cả ATA instruction và mint
        [],  
        blockhash,
    )
    
    msg_base64 = b64encode(bytes(msg)).decode("utf-8")

    return {
        "msg_base64": msg_base64,
        "recent_blockhash": str(blockhash),
        "position_nft_mint": str(accounts["position_nft_mint"]),
        "position_nft_account": str(accounts["position_nft_account"]),
        "personal_position": str(accounts["personal_position"]),
    }

def build_mint_position_tx_sol_v2(
    client: Client,
    payer_pubkey: Pubkey,
    program_id: Pubkey,
    position_nft_owner: Pubkey,
    pool_state: Pubkey,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = False,
    base_flag: bool | None = None
):
    """
    Build full tx for open_position_with_token22_nft:
    - Creates NFT mint, NFT account, personal position
    - Checks/creates ATA for token0/token1
    - Returns ready-to-send msg_base64
    """
    
    program_pubkey = program_id if isinstance(program_id, Pubkey) else Pubkey.from_string(program_id)
    payer_pubkey = payer_pubkey if isinstance(payer_pubkey, Pubkey) else Pubkey.from_string(payer_pubkey)
    position_nft_owner = position_nft_owner if isinstance(position_nft_owner, Pubkey) else Pubkey.from_string(position_nft_owner)
    pool_state = pool_state if isinstance(pool_state, Pubkey) else Pubkey.from_string(pool_state)
    
    # 1️⃣ Prepare PDAs / accounts
    position_nft_mint = Keypair()  # mỗi lần tạo mint mới
    res = client.get_account_info(position_nft_mint.pubkey())
    print(res)

    
    position_nft_account_pubkey = get_associated_token_address(
        owner=position_nft_owner,
        mint=position_nft_mint.pubkey(),
        token_program_id=TOKEN_2022_PROGRAM_ID
    ) 
    
    instructions = []

    # 2️⃣ Create NFT mint account
    rent_lamports = client.get_minimum_balance_for_rent_exemption(82).value
    instructions.append(create_account(
        CreateAccountParams(
            from_pubkey=payer_pubkey,
            to_pubkey=position_nft_mint.pubkey(),
            lamports=rent_lamports,
            space=82,
            owner=TOKEN_2022_PROGRAM_ID,
        )
    ))
    
    # Initialize mint (decimals=0)
    instructions.append(initialize_mint(
        InitializeMintParams(
            mint=position_nft_mint.pubkey(),   # mint account
            decimals=0,                        # NFT -> 0 decimals
            mint_authority=payer_pubkey,       # authority
            freeze_authority=payer_pubkey,     # authority
            program_id=TOKEN_2022_PROGRAM_ID,
        )
    ))

    # 3️⃣ Create position NFT account (associated to owner)
    try:
        info = client.get_account_info(position_nft_account_pubkey)
    except Exception:
        info = None

    if not info or not info.value:
        # ATA chưa tồn tại, tạo
        instructions.append(create_associated_token_account(
            payer=payer_pubkey,
            owner=position_nft_owner,
            mint=position_nft_mint.pubkey(),
            token_program_id=TOKEN_2022_PROGRAM_ID
        ))
    else:
        # ATA đã tồn tại, skip tạo
        print(f"ATA {position_nft_account_pubkey} already exists, skipping creation.")

    # 5️⃣ Create personal position
    personal_position, bump = Pubkey.find_program_address(
        [
            b"position",
            bytes(position_nft_mint.pubkey())
        ],
        program_pubkey
    )
    
    accounts = {
        "payer": payer_pubkey,
        "position_nft_owner": position_nft_owner,
        "position_nft_mint": position_nft_mint.pubkey(),
        "position_nft_account": position_nft_account_pubkey,
        "pool_state": pool_state,
        "protocol_position": Pubkey.from_string(params["protocol_position"]),
        "tick_array_lower": Pubkey.from_string(params["tick_array_lower"]),
        "tick_array_upper": Pubkey.from_string(params["tick_array_upper"]),
        "personal_position": personal_position,
        "token_account_0": Pubkey.from_string(params["token_account_0"]),
        "token_account_1": Pubkey.from_string(params["token_account_1"]),
        "token_vault_0": Pubkey.from_string(params["token_vault_0"]),
        "token_vault_1": Pubkey.from_string(params["token_vault_1"]),
        "vault_0_mint": Pubkey.from_string(params["token0_mint"]),
        "vault_1_mint": Pubkey.from_string(params["token1_mint"]),
    }

    # 4️⃣ Check and create ATA for token0/token1 if missing
    # Calculate ATA for token0 / token1
    ata_token0 = get_associated_token_address(position_nft_owner, accounts["vault_0_mint"])
    ata_token1 = get_associated_token_address(position_nft_owner, accounts["vault_1_mint"])

    # Check ATA 0
    info0 = client.get_account_info(ata_token0)
    if not info0.value:
        instructions.append(
            create_associated_token_account(
                payer=payer_pubkey,
                owner=position_nft_owner,
                mint=accounts["vault_0_mint"],
            )
        )

    # Check ATA 1
    info1 = client.get_account_info(ata_token1)
    if not info1.value:
        instructions.append(
            create_associated_token_account(
                payer=payer_pubkey,
                owner=position_nft_owner,
                mint=accounts["vault_1_mint"],
            )
        )

    # Update accounts to use actual ATA
    accounts["token_account_0"] = ata_token0
    accounts["token_account_1"] = ata_token1

    
    # 5️⃣ Encode instruction args for open_position_with_token22_nft
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])  # Anchor discriminator
    data = bytearray(discriminator)
    data += struct.pack("<i", params["tick_lower_index"])
    data += struct.pack("<i", params["tick_upper_index"])
    data += struct.pack("<i", params["tick_array_lower_start_index"])
    data += struct.pack("<i", params["tick_array_upper_start_index"])
    data += struct.pack("<Q", liquidity & ((1 << 64) - 1))
    data += struct.pack("<Q", liquidity >> 64)
    data += struct.pack("<Q", amount_0_max)
    data += struct.pack("<Q", amount_1_max)
    data += struct.pack("<?", with_metadata)
    if base_flag is None:
        data += struct.pack("<?", False)
    else:
        data += struct.pack("<?", True)
        data += struct.pack("<?", base_flag)

    # 6️⃣ Build keys
    keys = [
        AccountMeta(accounts["payer"], True, True),
        AccountMeta(accounts["position_nft_owner"], False, False),
        AccountMeta(accounts["position_nft_mint"], True, True),
        AccountMeta(accounts["position_nft_account"], False, True),
        AccountMeta(accounts["pool_state"], False, True),
        AccountMeta(accounts["protocol_position"], False, True),
        AccountMeta(accounts["tick_array_lower"], False, True),
        AccountMeta(accounts["tick_array_upper"], False, True),
        AccountMeta(accounts["personal_position"], False, True),
        AccountMeta(accounts["token_account_0"], False, True),
        AccountMeta(accounts["token_account_1"], False, True),
        AccountMeta(accounts["token_vault_0"], False, True),
        AccountMeta(accounts["token_vault_1"], False, True),
        AccountMeta(RENT, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_PROGRAM_ID, False, False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, False, False),
        AccountMeta(accounts["vault_0_mint"], False, False),
        AccountMeta(accounts["vault_1_mint"], False, False),
    ]

    # 7️⃣ Create main instruction
    ix = Instruction(program_pubkey, bytes(data), keys)
    instructions.append(ix)

    # 8️⃣ Compile to MessageV0
    blockhash = client.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer_pubkey,
        instructions,
        [],
        blockhash
    )

    msg_base64 = b64encode(bytes(msg)).decode("utf-8")

    return {
        "msg_base64": msg_base64,
        "recent_blockhash": str(blockhash),
        "position_nft_mint": str(position_nft_mint.pubkey()),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
    }

def build_mint_position_tx_sol_v4(
    client,
    payer_pubkey,
    program_id,
    position_nft_owner,
    pool_state,
    params,
    amount_0_max,
    amount_1_max,
    liquidity=0,
    with_metadata=False,
    base_flag=None
):
    """
    V5: Tách transaction
    - Tx1: Tạo NFT mint + position NFT ATA
    - Tx2: Open position với Pancake program
    """
    program_pubkey = program_id if isinstance(program_id, Pubkey) else Pubkey.from_string(program_id)
    payer_pubkey = payer_pubkey if isinstance(payer_pubkey, Pubkey) else Pubkey.from_string(payer_pubkey)
    position_nft_owner = position_nft_owner if isinstance(position_nft_owner, Pubkey) else Pubkey.from_string(position_nft_owner)
    pool_state = pool_state if isinstance(pool_state, Pubkey) else Pubkey.from_string(pool_state)

    # -----------------------
    # 1️⃣ Tx1: Mint + NFT ATA
    # -----------------------
    position_nft_mint = Keypair()
    position_nft_account_pubkey = get_associated_token_address(
        owner=position_nft_owner,
        mint=position_nft_mint.pubkey(),
        token_program_id=TOKEN_2022_PROGRAM_ID
    )

    rent_lamports = client.get_minimum_balance_for_rent_exemption(82).value
    instructions_tx1 = []

    # Create NFT mint
    instructions_tx1.append(create_account(
        CreateAccountParams(
            from_pubkey=payer_pubkey,
            to_pubkey=position_nft_mint.pubkey(),
            lamports=rent_lamports,
            space=82,
            owner=TOKEN_2022_PROGRAM_ID,
        )
    ))
    instructions_tx1.append(initialize_mint(
        InitializeMintParams(
            mint=position_nft_mint.pubkey(),
            decimals=0,
            mint_authority=payer_pubkey,
            freeze_authority=payer_pubkey,
            program_id=TOKEN_2022_PROGRAM_ID,
        )
    ))

    # Create position NFT ATA (if missing)
    info = client.get_account_info(position_nft_account_pubkey)
    if not info.value:
        instructions_tx1.append(create_associated_token_account(
            payer=payer_pubkey,
            owner=position_nft_owner,
            mint=position_nft_mint.pubkey(),
            token_program_id=TOKEN_2022_PROGRAM_ID
        ))

    blockhash1 = client.get_latest_blockhash().value.blockhash
    msg_tx1 = MessageV0.try_compile(payer_pubkey, instructions_tx1, [], blockhash1)
    msg_base64_tx1 = b64encode(bytes(msg_tx1)).decode("utf-8")

    # -----------------------
    # 2️⃣ Tx2: Open Position
    # -----------------------
    # Compute personal position PDA
    personal_position, _ = Pubkey.find_program_address(
        [b"position", bytes(position_nft_mint.pubkey())],
        program_pubkey
    )

    # Check/Create token0/token1 ATA
    vault_0_mint = Pubkey.from_string(params["token0_mint"])
    vault_1_mint = Pubkey.from_string(params["token1_mint"])
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint)

    instructions_tx2 = []
    if not client.get_account_info(ata_token0).value:
        instructions_tx2.append(create_associated_token_account(
            payer=payer_pubkey,
            owner=position_nft_owner,
            mint=vault_0_mint
        ))
    if not client.get_account_info(ata_token1).value:
        instructions_tx2.append(create_associated_token_account(
            payer=payer_pubkey,
            owner=position_nft_owner,
            mint=vault_1_mint
        ))

    # Encode open_position_with_token22_nft
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)
    data += struct.pack("<i", params["tick_lower_index"])
    data += struct.pack("<i", params["tick_upper_index"])
    data += struct.pack("<i", params["tick_array_lower_start_index"])
    data += struct.pack("<i", params["tick_array_upper_start_index"])
    data += struct.pack("<Q", liquidity & ((1 << 64) - 1))
    data += struct.pack("<Q", liquidity >> 64)
    data += struct.pack("<Q", amount_0_max)
    data += struct.pack("<Q", amount_1_max)
    data += struct.pack("<?", with_metadata)
    if base_flag is None:
        data += struct.pack("<?", False)
    else:
        data += struct.pack("<?", True)
        data += struct.pack("<?", base_flag)

    # Accounts for instruction
    accounts = {
        "payer": payer_pubkey,
        "position_nft_owner": position_nft_owner,
        "position_nft_mint": position_nft_mint.pubkey(),
        "position_nft_account": position_nft_account_pubkey,
        "pool_state": pool_state,
        "protocol_position": Pubkey.from_string(params["protocol_position"]),
        "tick_array_lower": Pubkey.from_string(params["tick_array_lower"]),
        "tick_array_upper": Pubkey.from_string(params["tick_array_upper"]),
        "personal_position": personal_position,
        "token_account_0": ata_token0,
        "token_account_1": ata_token1,
        "token_vault_0": Pubkey.from_string(params["token_vault_0"]),
        "token_vault_1": Pubkey.from_string(params["token_vault_1"]),
        "vault_0_mint": vault_0_mint,
        "vault_1_mint": vault_1_mint,
    }

    keys = [
        AccountMeta(accounts["payer"], True, True),
        AccountMeta(accounts["position_nft_owner"], False, False),
        AccountMeta(accounts["position_nft_mint"], True, True),
        AccountMeta(accounts["position_nft_account"], False, True),
        AccountMeta(accounts["pool_state"], False, True),
        AccountMeta(accounts["protocol_position"], False, True),
        AccountMeta(accounts["tick_array_lower"], False, True),
        AccountMeta(accounts["tick_array_upper"], False, True),
        AccountMeta(accounts["personal_position"], False, True),
        AccountMeta(accounts["token_account_0"], False, True),
        AccountMeta(accounts["token_account_1"], False, True),
        AccountMeta(accounts["token_vault_0"], False, True),
        AccountMeta(accounts["token_vault_1"], False, True),
        AccountMeta(RENT, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_PROGRAM_ID, False, False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, False, False),
        AccountMeta(accounts["vault_0_mint"], False, False),
        AccountMeta(accounts["vault_1_mint"], False, False),
    ]

    instructions_tx2.append(Instruction(program_pubkey, bytes(data), keys))

    blockhash2 = client.get_latest_blockhash().value.blockhash
    msg_tx2 = MessageV0.try_compile(payer_pubkey, instructions_tx2, [], blockhash2)
    msg_base64_tx2 = b64encode(bytes(msg_tx2)).decode("utf-8")

    # -----------------------
    # Return full info
    # -----------------------
    return {
        "tx1_base64": msg_base64_tx1,
        "tx2_base64": msg_base64_tx2,
        "recent_blockhash_tx1": str(blockhash1),
        "recent_blockhash_tx2": str(blockhash2),
        "position_nft_mint": str(position_nft_mint.pubkey()),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "token_account_0": str(ata_token0),
        "token_account_1": str(ata_token1),
    }

def build_open_position_tx_v5(
    client,                     # Async client (RPC) with methods get_minimum_balance_for_rent_exemption, get_latest_blockhash, get_account_info
    payer_pubkey,               # Pubkey or str of payer (fee payer)
    position_nft_owner,         # Pubkey or str of user who will own the NFT (also signer for transfer)
    pool_state,                 # Pubkey or str of the pool state account
    params: dict,               # dict containing required pool params (see usage below)
    amount_0_max: int,          # amount of token0 to deposit in base units
    amount_1_max: int,          # amount of token1 to deposit in base units
    liquidity: int = 0,
    with_metadata: bool = False,
    base_flag: bool | None = None,
    add_create_mint: bool = True,   # if True: include create_account for position_nft_mint in same tx
):
    """
    Build a single-message V0 transaction (base64) for open_position_with_token22_nft.

    Returns a dict:
      {
        "tx_base64": "<base64 message>",    # message to be signed by required signers
        "required_signers": [pubkey_strs...],
        "position_nft_mint": "<mint pubkey>",
        "position_nft_account": "<associated token account for NFT>",
        "personal_position": "<derived personal position PDA>"
      }

    NOTES:
    - This builds 1 transaction (not two). It does:
        1) optionally create_account for position_nft_mint (allocated, owner = TOKEN_2022_PROGRAM_ID)
        2) transfer_checked token0 -> pool_vault_0
        3) transfer_checked token1 -> pool_vault_1
        4) OpenPositionWithToken22Nft instruction
    - Do NOT create NFT ATA here: program CPI will create and initialize mint & ATA & mintTo according to logs.
    - Caller/wallet must sign for: payer, position_nft_owner (for transfer), and position_nft_mint (if create_account used).
    """

    # Normalize pubkeys
    program_pubkey = PANCAKE_PROGRAM_ID
    payer_pubkey = payer_pubkey if isinstance(payer_pubkey, Pubkey) else Pubkey.from_string(str(payer_pubkey))
    position_nft_owner = position_nft_owner if isinstance(position_nft_owner, Pubkey) else Pubkey.from_string(str(position_nft_owner))
    pool_state = pool_state if isinstance(pool_state, Pubkey) else Pubkey.from_string(str(pool_state))

    # Required params keys expected in `params`:
    # token0_mint, token1_mint, token_vault_0, token_vault_1,
    # protocol_position (pubkey), tick_array_lower, tick_array_upper,
    # tick_array_lower_start_index, tick_array_upper_start_index,
    # tick_lower_index, tick_upper_index
    # (Make sure all present)
    
    assert "token0_mint" in params and "token1_mint" in params, "params must include token mints"
    assert "token_vault_0" in params and "token_vault_1" in params, "params must include vault pubkeys"
    assert "protocol_position" in params and "tick_array_lower" in params and "tick_array_upper" in params, "missing PDAs"

    vault_0_mint = Pubkey.from_string(str(params["token0_mint"]))
    vault_1_mint = Pubkey.from_string(str(params["token1_mint"]))
    pool_vault_0 = Pubkey.from_string(str(params["token_vault_0"]))
    pool_vault_1 = Pubkey.from_string(str(params["token_vault_1"]))

    # Derive a fresh mint keypair for position NFT (client provides keypair; program will initialize it via CPI)
    position_nft_mint_kp = Keypair()
    position_nft_mint_pubkey = position_nft_mint_kp.pubkey()
    print("position_nft_mint_pubkey:", position_nft_mint_pubkey)

    # Derive personal_position PDA used by program
    personal_position, _ = Pubkey.find_program_address([b"position", bytes(position_nft_mint_pubkey)], program_pubkey)

    # Derive associated token account for NFT (owner's ATA for the NFT mint) — NOTE: program may create it, but we provide expected address
    position_nft_account_pubkey = get_associated_token_address(position_nft_owner, position_nft_mint_pubkey, token_program_id=TOKEN_2022_PROGRAM_ID)

    # User token ATAs (must exist or we can include create ATA instructions for them)
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint)

    # === Build instructions list ===
    instructions = []

    # 1) (Optional) create_account for position_nft_mint (allocate space but DO NOT initialize mint here).
    if add_create_mint:
        # rent-exempt amount for Token-2022 mint depends on layout; we approximate with 82 as your earlier code
        mint_space = 82  # use exact size used by Token-2022 implementation
        rent_resp = client.get_minimum_balance_for_rent_exemption(mint_space)
        rent_lamports = rent_resp.value if hasattr(rent_resp, "value") else rent_resp["result"]
        create_mint_ix = create_account(
            CreateAccountParams(
                from_pubkey=payer_pubkey,
                to_pubkey=position_nft_mint_pubkey,
                lamports=rent_lamports,
                space=mint_space,
                owner=TOKEN_2022_PROGRAM_ID,
            )
        )
        instructions.append(create_mint_ix)
        # DO NOT call initialize_mint here — program will do initialize via CPI per log.

    # 2) (Optionally) ensure user's token ATAs exist — if not, include create_associated_token_account.
    #    You can add these instructions if you want the tx to create them. If you prefer to fail if missing, remove creation.
    info0 = client.get_account_info(ata_token0)
    if not info0.value:
        # create user ATA for token0
        from spl.token.instructions import create_associated_token_account
        instructions.append(create_associated_token_account(payer_pubkey, position_nft_owner, vault_0_mint))

    info1 = client.get_account_info(ata_token1)
    if not info1.value:
        from spl.token.instructions import create_associated_token_account
        instructions.append(create_associated_token_account(payer_pubkey, position_nft_owner, vault_1_mint))

    # 3) TransferChecked token0 -> pool_vault_0
    # NOTE: you must provide correct decimals for each token. If unknown, pass decimals via params or lookup on-chain.
    decimals_0 = params.get("decimals_0", 9)
    decimals_1 = params.get("decimals_1", 9)

    transfer0_ix = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=ata_token0,
            mint=vault_0_mint,
            dest=pool_vault_0,
            owner=position_nft_owner,
            amount=amount_0_max,
            decimals=decimals_0,
        )
    )
    instructions.append(transfer0_ix)

    transfer1_ix = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=ata_token1,
            mint=vault_1_mint,
            dest=pool_vault_1,
            owner=position_nft_owner,
            amount=amount_1_max,
            decimals=decimals_1,
        )
    )
    instructions.append(transfer1_ix)

    # 4) Build OpenPositionWithToken22Nft instruction data (discriminator + params)
    #    Use the same layout you used earlier (discriminator + ints)
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])  # keep your discriminator
    data = bytearray(discriminator)
    # pack tick indices and tick array start indices and liquidity (128 split) and amounts (u64) and booleans
    data += struct.pack("<i", int(params["tick_lower_index"]))
    data += struct.pack("<i", int(params["tick_upper_index"]))
    data += struct.pack("<i", int(params["tick_array_lower_start_index"]))
    data += struct.pack("<i", int(params["tick_array_upper_start_index"]))
    # liquidity 128-bit little endian as two u64 (low, high)
    liquidity_low = liquidity & ((1 << 64) - 1)
    liquidity_high = (liquidity >> 64) & ((1 << 64) - 1)
    data += struct.pack("<Q", liquidity_low)
    data += struct.pack("<Q", liquidity_high)
    data += struct.pack("<Q", int(amount_0_max))
    data += struct.pack("<Q", int(amount_1_max))
    data += struct.pack("<?", bool(with_metadata))
    if base_flag is None:
        data += struct.pack("<?", False)
    else:
        data += struct.pack("<?", True)
        data += struct.pack("<?", bool(base_flag))

    # 5) Build accounts list for OpenPosition instruction (match what program expects)
    accounts_map = {
        "payer": payer_pubkey,
        "position_nft_owner": position_nft_owner,
        "position_nft_mint": position_nft_mint_pubkey,
        "position_nft_account": position_nft_account_pubkey,
        "pool_state": pool_state,
        "protocol_position": Pubkey.from_string(str(params["protocol_position"])),
        "tick_array_lower": Pubkey.from_string(str(params["tick_array_lower"])),
        "tick_array_upper": Pubkey.from_string(str(params["tick_array_upper"])),
        "personal_position": personal_position,
        "token_account_0": ata_token0,
        "token_account_1": ata_token1,
        "token_vault_0": pool_vault_0,
        "token_vault_1": pool_vault_1,
        "vault_0_mint": vault_0_mint,
        "vault_1_mint": vault_1_mint,
    }

    keys = [
        AccountMeta(accounts_map["payer"], is_signer=True, is_writable=True),
        AccountMeta(accounts_map["position_nft_owner"], is_signer=True, is_writable=False),
        # position_nft_mint must be signer if we created it (Keypair) so program can initialize; if not created client must supply its pubkey and
        # program will create/initialize via CPI (depends on AMM IDL). We mark writable; signer True if we passed Keypair.
        AccountMeta(accounts_map["position_nft_mint"], is_signer=add_create_mint, is_writable=True),
        AccountMeta(accounts_map["position_nft_account"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["pool_state"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["protocol_position"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["tick_array_lower"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["tick_array_upper"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["personal_position"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["token_account_0"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["token_account_1"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["token_vault_0"], is_signer=False, is_writable=True),
        AccountMeta(accounts_map["token_vault_1"], is_signer=False, is_writable=True),
        AccountMeta(RENT, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(accounts_map["vault_0_mint"], is_signer=False, is_writable=False),
        AccountMeta(accounts_map["vault_1_mint"], is_signer=False, is_writable=False),
    ]

    open_pos_ix = Instruction(program_pubkey, bytes(data), keys)
    instructions.append(open_pos_ix)

    # === Compile MessageV0 ===
    # Get recent blockhash
    blockhash_resp = client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash if hasattr(blockhash_resp, "value") else blockhash_resp["result"]["value"]["blockhash"]

    msg = MessageV0.try_compile(payer_pubkey, instructions, [], recent_blockhash)
    msg_base64 = b64encode(bytes(msg)).decode("utf-8")

    # Determine required signers (pubkey strings) so the caller knows who must sign the message
    required_signers = [str(payer_pubkey), str(position_nft_owner)]
    if add_create_mint:
        # The mint keypair must sign as it is included as signer in account metas.
        required_signers.append(str(position_nft_mint_pubkey))

    return {
        "tx_base64": msg_base64,
        "required_signers": required_signers,
        "position_nft_mint": str(position_nft_mint_pubkey),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "recent_blockhash": str(recent_blockhash),
    }

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IDL_PATH = os.path.join(BASE_DIR, 'idl', 'pancake_position_idl.json')

COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

def set_compute_unit_price_ix(lamports_per_cu: int) -> Instruction:
    data = lamports_per_cu.to_bytes(8, "little")
    return Instruction(COMPUTE_BUDGET_PROGRAM_ID, data, [])

def set_compute_unit_limit_ix(limit: int) -> Instruction:
    # format: u8(0) + u32(limit)
    data = bytes([0]) + limit.to_bytes(4, "little")
    return Instruction(COMPUTE_BUDGET_PROGRAM_ID, data, [])

def transfer_checked_ix(source, dest, mint, owner, amount, decimals):
    from spl.token.instructions import transfer_checked, TransferCheckedParams
    return transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=source,
            mint=mint,
            dest=dest,
            owner=owner,
            amount=amount,
            decimals=decimals,
        )
    )

def create_account_ix_for_nft(payer_pubkey, new_mint_pubkey):
    # rent-exempt lamports
    rent_lamports = 1_000_000  # estimate; should query client
    return create_account(
        CreateAccountParams(
            from_pubkey=payer_pubkey,
            to_pubkey=new_mint_pubkey,
            lamports=rent_lamports,
            space=82,
            owner=TOKEN_2022_PROGRAM_ID,
        )
    )

def build_mint_position_tx_sol_v7(
    client,
    payer_pubkey,
    position_nft_owner,
    pool_state,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = True,
    base_flag: bool | None = True,
    add_create_mint: bool = True,
    transfer_tokens: bool = True,  # if True: transfer_checked before open_position
):
    instructions = []

    # --- Compute Budget ---
    instructions.append(set_compute_unit_price_ix(1))
    instructions.append(set_compute_unit_limit_ix(1_000_000))

    # --- Normalize pubkeys ---
    payer_pubkey = Pubkey.from_string(str(payer_pubkey)) if not isinstance(payer_pubkey, Pubkey) else payer_pubkey
    position_nft_owner = Pubkey.from_string(str(position_nft_owner)) if not isinstance(position_nft_owner, Pubkey) else position_nft_owner
    pool_state = Pubkey.from_string(str(pool_state)) if not isinstance(pool_state, Pubkey) else pool_state
    vault_0_mint = Pubkey.from_string(str(params["token0_mint"]))
    vault_1_mint = Pubkey.from_string(str(params["token1_mint"]))
    pool_vault_0 = Pubkey.from_string(str(params["token_vault_0"]))
    pool_vault_1 = Pubkey.from_string(str(params["token_vault_1"]))

    # --- User token ATA / source accounts ---
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint)

    # --- Handle SOL if either token is native SOL ---
    temp_wsol_account = None
    wrapped_sol_mint_str = str(WRAPPED_SOL_MINT)
    if str(vault_0_mint) == wrapped_sol_mint_str or str(vault_1_mint) == wrapped_sol_mint_str:
        wsol_amount = amount_0_max if str(vault_0_mint) == wrapped_sol_mint_str else amount_1_max
        temp_wsol_account = Keypair()
        seed = f"wrap_sol_{int(time.time())}"

        # 1) create temp WSOL account
        instructions.append(
            create_account_with_seed(
                CreateAccountWithSeedParams(
                    from_pubkey=payer_pubkey,
                    to_pubkey=temp_wsol_account.pubkey(),
                    base=payer_pubkey,
                    seed=seed,
                    lamports=wsol_amount,
                    space=165,
                    owner=TOKEN_PROGRAM_ID,
                )
            )
        )

        # 2) initialize account as WSOL
        instructions.append(
            initialize_account(
                InitializeAccountParams(
                    account=temp_wsol_account.pubkey(),
                    mint=SYSTEM_PROGRAM_ID,
                    owner=position_nft_owner,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # --- Determine transfer sources ---
    source_token0 = temp_wsol_account.pubkey() if str(vault_0_mint) == wrapped_sol_mint_str else ata_token0
    source_token1 = temp_wsol_account.pubkey() if str(vault_1_mint) == wrapped_sol_mint_str else ata_token1

    # --- Transfer tokens to pool vaults ---
    decimals_0 = params.get("decimals_0", 9)
    decimals_1 = params.get("decimals_1", 9)
    if transfer_tokens:
        instructions.append(transfer_checked_ix(source_token0, pool_vault_0, vault_0_mint, position_nft_owner, amount_0_max, decimals_0))
        instructions.append(transfer_checked_ix(source_token1, pool_vault_1, vault_1_mint, position_nft_owner, amount_1_max, decimals_1))

    # --- Position NFT mint + PDA ---
    position_nft_mint_kp = Keypair()
    position_nft_mint_pubkey = position_nft_mint_kp.pubkey()
    personal_position, _ = Pubkey.find_program_address([b"position", bytes(position_nft_mint_pubkey)], PANCAKE_PROGRAM_ID)
    position_nft_account_pubkey = get_associated_token_address(position_nft_owner, position_nft_mint_pubkey, token_program_id=TOKEN_2022_PROGRAM_ID)

    if add_create_mint:
        instructions.append(create_account_ix_for_nft(payer_pubkey, position_nft_mint_pubkey))

    # --- Build instruction data ---
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)
    data += struct.pack("<i", int(params["tick_lower_index"]))
    data += struct.pack("<i", int(params["tick_upper_index"]))
    data += struct.pack("<i", int(params["tick_array_lower_start_index"]))
    data += struct.pack("<i", int(params["tick_array_upper_start_index"]))
    liquidity_low = liquidity & ((1 << 64) - 1)
    liquidity_high = (liquidity >> 64) & ((1 << 64) - 1)
    data += struct.pack("<Q", liquidity_low)
    data += struct.pack("<Q", liquidity_high)
    data += struct.pack("<Q", amount_0_max)
    data += struct.pack("<Q", amount_1_max)
    data += struct.pack("<?", with_metadata)
    if base_flag is None:
        data += struct.pack("<?", False)
    else:
        data += struct.pack("<?", True)
        data += struct.pack("<?", bool(base_flag))

    # --- Accounts for open_position instruction ---
    keys = [
        AccountMeta(payer_pubkey, is_signer=True, is_writable=True),
        AccountMeta(position_nft_owner, is_signer=True, is_writable=True),
        AccountMeta(position_nft_mint_pubkey, is_signer=add_create_mint, is_writable=True),
        AccountMeta(position_nft_account_pubkey, is_signer=False, is_writable=True),
        AccountMeta(pool_state, is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(params["protocol_position"]), is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(params["tick_array_lower"]), is_signer=False, is_writable=True),
        AccountMeta(Pubkey.from_string(params["tick_array_upper"]), is_signer=False, is_writable=True),
        AccountMeta(personal_position, is_signer=False, is_writable=True),
        AccountMeta(source_token0, is_signer=False, is_writable=True),   # source token0
        AccountMeta(source_token1, is_signer=False, is_writable=True),   # source token1
        AccountMeta(pool_vault_0, is_signer=False, is_writable=True),
        AccountMeta(pool_vault_1, is_signer=False, is_writable=True),
        AccountMeta(RENT, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(vault_0_mint, is_signer=False, is_writable=False),
        AccountMeta(vault_1_mint, is_signer=False, is_writable=False),
    ]

    open_pos_ix = Instruction(PANCAKE_PROGRAM_ID, bytes(data), keys)
    instructions.append(open_pos_ix)
    
    if temp_wsol_account is not None:
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=temp_wsol_account.pubkey(),
                    dest=position_nft_owner,  # Hoàn lại rent cho owner
                    owner=position_nft_owner,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # === Compile MessageV0 ===
    # Get recent blockhash
    blockhash_resp = client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash if hasattr(blockhash_resp, "value") else blockhash_resp["result"]["value"]["blockhash"]

    msg = MessageV0.try_compile(payer_pubkey, instructions, [], recent_blockhash)
    msg_base64 = b64encode(bytes(msg)).decode("utf-8")

    required_signers = [str(payer_pubkey), str(position_nft_owner)]
    if add_create_mint:
        required_signers.append(str(position_nft_mint_pubkey))

    return {
        "msg_base64": msg_base64,
        "required_signers": required_signers,
        "position_nft_mint": str(position_nft_mint_pubkey),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "recent_blockhash": str(recent_blockhash),
    }

def build_mint_position_tx_sol_v8(
    client,
    payer_pubkey,
    position_nft_owner,
    pool_state,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = True,
    base_flag: bool | None = True,
):
    """
    Build mint position transaction for PancakeSwap V3 on Solana.
    
    Args:
        client: Solana RPC client
        payer_pubkey: Account that pays for transaction fees
        position_nft_owner: Owner of the position NFT (must sign)
        pool_state: Pool state pubkey
        params: Dict containing pool parameters (tick indexes, token mints, vaults, etc.)
        amount_0_max: Maximum amount of token0 to deposit
        amount_1_max: Maximum amount of token1 to deposit
        liquidity: Liquidity amount (0 = calculate based on base_flag)
        with_metadata: Whether to create NFT metadata
        base_flag: Calculate liquidity based on amount_0 (True) or amount_1 (False), None = use liquidity param
    
    Returns:
        Dict with message, signers, and position info
    """
    instructions = []

    # --- Compute Budget ---
    instructions.append(set_compute_unit_price(1_000))
    instructions.append(set_compute_unit_limit(250_000))

    # --- Normalize pubkeys ---
    payer_pubkey = Pubkey.from_string(str(payer_pubkey)) if not isinstance(payer_pubkey, Pubkey) else payer_pubkey
    position_nft_owner = Pubkey.from_string(str(position_nft_owner)) if not isinstance(position_nft_owner, Pubkey) else position_nft_owner
    pool_state = Pubkey.from_string(str(pool_state)) if not isinstance(pool_state, Pubkey) else pool_state
    vault_0_mint = Pubkey.from_string(str(params["token0_mint"]))
    vault_1_mint = Pubkey.from_string(str(params["token1_mint"]))
    pool_vault_0 = Pubkey.from_string(str(params["token_vault_0"]))
    pool_vault_1 = Pubkey.from_string(str(params["token_vault_1"]))

    # --- User token ATA ---
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint)

    # --- Handle WSOL wrapping ---
    temp_wsol_account = None
    wrapped_sol_mint_str = str(WRAPPED_SOL_MINT)
    
    if str(vault_0_mint) == wrapped_sol_mint_str or str(vault_1_mint) == wrapped_sol_mint_str:
        wsol_amount = amount_0_max if str(vault_0_mint) == wrapped_sol_mint_str else amount_1_max
        
        # Generate unique seed for WSOL account
        seed = f"wsol_{int(time.time() * 1000)}"
        
        # Derive WSOL account address from seed
        temp_wsol_account = Pubkey.create_with_seed(payer_pubkey, seed, TOKEN_PROGRAM_ID)
        
        # 1. Rent exemption
        rent_lamports = client.get_minimum_balance_for_rent_exemption(165).value
        
        # 1) Create temp WSOL account with seed
        instructions.append(
            create_account_with_seed(
                CreateAccountWithSeedParams(
                    from_pubkey=payer_pubkey,
                    to_pubkey=temp_wsol_account,
                    base=payer_pubkey,
                    seed=seed,
                    lamports=rent_lamports + wsol_amount,
                    space=165,
                    owner=TOKEN_PROGRAM_ID,
                )
            )
        )
        
        # 2) Initialize as WSOL token account
        instructions.append(
            initialize_account(
                InitializeAccountParams(
                    account=temp_wsol_account,
                    mint=WRAPPED_SOL_MINT,
                    owner=payer_pubkey,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # --- Determine source accounts for tokens ---
    source_token0 = temp_wsol_account if str(vault_0_mint) == wrapped_sol_mint_str else ata_token0
    source_token1 = temp_wsol_account if str(vault_1_mint) == wrapped_sol_mint_str else ata_token1

    # --- Position NFT mint keypair (MUST BE SIGNER per IDL) ---
    position_nft_mint_kp = Keypair()
    position_nft_mint_pubkey = position_nft_mint_kp.pubkey()
    
    # --- Derive PDAs ---
    personal_position, _ = Pubkey.find_program_address(
        [b"position", bytes(position_nft_mint_pubkey)], 
        PANCAKE_PROGRAM_ID
    )
    position_nft_account_pubkey = get_associated_token_address(
        position_nft_owner, 
        position_nft_mint_pubkey, 
        token_program_id=TOKEN_2022_PROGRAM_ID
    )

    # --- Build open_position instruction data ---
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)
    
    # tick_lower_index: i32
    data += struct.pack("<i", int(params["tick_lower_index"]))
    # tick_upper_index: i32
    data += struct.pack("<i", int(params["tick_upper_index"]))
    # tick_array_lower_start_index: i32
    data += struct.pack("<i", int(params["tick_array_lower_start_index"]))
    # tick_array_upper_start_index: i32
    data += struct.pack("<i", int(params["tick_array_upper_start_index"]))
    
    # liquidity: u128 (16 bytes, little-endian)
    liquidity_low = liquidity & ((1 << 64) - 1)
    liquidity_high = (liquidity >> 64) & ((1 << 64) - 1)
    data += struct.pack("<Q", liquidity_low)
    data += struct.pack("<Q", liquidity_high)
    
    # amount_0_max: u64
    data += struct.pack("<Q", amount_0_max)
    # amount_1_max: u64
    data += struct.pack("<Q", amount_1_max)
    
    # with_metadata: bool
    data += struct.pack("<?", with_metadata)
    
    # base_flag: Option<bool>
    if base_flag is None:
        data += struct.pack("<?", False)  # None variant
    else:
        data += struct.pack("<?", True)   # Some variant
        data += struct.pack("<?", bool(base_flag))

    # --- Build accounts list (MUST MATCH IDL ORDER) ---
    keys = [
        # 0. payer
        AccountMeta(payer_pubkey, is_signer=True, is_writable=True),
        # 1. position_nft_owner
        AccountMeta(position_nft_owner, is_signer=True, is_writable=False),
        # 2. position_nft_mint (MUST BE SIGNER per IDL!)
        AccountMeta(position_nft_mint_pubkey, is_signer=True, is_writable=True),
        # 3. position_nft_account
        AccountMeta(position_nft_account_pubkey, is_signer=False, is_writable=True),
        # 4. pool_state
        AccountMeta(pool_state, is_signer=False, is_writable=True),
        # 5. protocol_position
        AccountMeta(Pubkey.from_string(params["protocol_position"]), is_signer=False, is_writable=True),
        # 6. tick_array_lower
        AccountMeta(Pubkey.from_string(params["tick_array_lower"]), is_signer=False, is_writable=True),
        # 7. tick_array_upper
        AccountMeta(Pubkey.from_string(params["tick_array_upper"]), is_signer=False, is_writable=True),
        # 8. personal_position
        AccountMeta(personal_position, is_signer=False, is_writable=True),
        # 9. token_account_0 (user's source for token0)
        AccountMeta(source_token0, is_signer=False, is_writable=True),
        # 10. token_account_1 (user's source for token1)
        AccountMeta(source_token1, is_signer=False, is_writable=True),
        # 11. token_vault_0
        AccountMeta(pool_vault_0, is_signer=False, is_writable=True),
        # 12. token_vault_1
        AccountMeta(pool_vault_1, is_signer=False, is_writable=True),
        # 13. rent
        AccountMeta(RENT, is_signer=False, is_writable=False),
        # 14. system_program
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        # 15. token_program
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        # 16. associated_token_program
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        # 17. token_program_2022
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        # 18. vault_0_mint
        AccountMeta(vault_0_mint, is_signer=False, is_writable=False),
        # 19. vault_1_mint
        AccountMeta(vault_1_mint, is_signer=False, is_writable=False),
    ]
    
    tick_array_bitmap_extension_account = get_tick_array_bitmap_account_from_db(pool_id=str(pool_state), tick_lower=params["tick_lower_index"], tick_upper=params["tick_upper_index"])
    if tick_array_bitmap_extension_account is not None:
        keys.append(AccountMeta(Pubkey.from_string(tick_array_bitmap_extension_account), is_signer=False, is_writable=False))

    # Add open_position instruction
    open_pos_ix = Instruction(PANCAKE_PROGRAM_ID, bytes(data), keys)
    instructions.append(open_pos_ix)
    
    # --- Close temp WSOL account if created ---
    if temp_wsol_account is not None:
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=temp_wsol_account,
                    dest=payer_pubkey,  # Return rent to owner
                    owner=payer_pubkey,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # === Compile MessageV0 ===
    blockhash_resp = client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash if hasattr(blockhash_resp, "value") else blockhash_resp["result"]["value"]["blockhash"]

    msg = MessageV0.try_compile(payer_pubkey, instructions, [], recent_blockhash)
    # ✅ SERIALIZE MESSAGE + POSITION_NFT_MINT KEYPAIR
    # Client will handle signing both
    raw_msg = bytes(msg)
    msg_base64 = b64encode(bytes(msg)).decode("utf-8")
    
    # Serialize position_nft_mint keypair secret key for client
    position_nft_mint_secret = list(bytes(position_nft_mint_kp))  # 64 bytes
    
    print("=== DEBUG INFO ===")
    print(f"Number of instructions: {len(instructions)}")
    print(f"Position NFT mint: {position_nft_mint_pubkey}")
    print(f"Message accounts: {len(msg.account_keys)}")
    print(f"Signers in message: {[str(k) for i, k in enumerate(msg.account_keys) if msg.is_signer(i)]}")
    print(f"Message size (bytes): {len(raw_msg)}")
    
    # Serialize transaction (already has 1 signature)
    
    return {
        "msg_base64": msg_base64,  # Message (unsigned)
        "position_nft_mint_secret": position_nft_mint_secret,  # Secret key for client to sign
        "required_signers": [str(payer_pubkey), str(position_nft_mint_pubkey)],
        "position_nft_mint": str(position_nft_mint_pubkey),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "recent_blockhash": str(recent_blockhash),
    }

from base64 import b64encode

from solders.transaction import VersionedTransaction
from solders.null_signer import NullSigner
from solders.message import MessageV0

def detect_token_program(client, mint: Pubkey) -> Pubkey:
    resp = client.get_account_info(mint)
    if resp.value is None:
        raise ValueError("Mint account does not exist")

    owner = resp.value.owner

    if owner == TOKEN_PROGRAM_ID:
        return TOKEN_PROGRAM_ID
    elif owner == TOKEN_2022_PROGRAM_ID:
        return TOKEN_2022_PROGRAM_ID
    else:
        raise ValueError(f"Unknown token program: {owner}")

def build_mint_position_tx_sol_v9(
    client,
    payer_pubkey,
    position_nft_owner,
    pool_state,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = True,
    base_flag: bool | None = True,
):
    # --- toàn bộ phần trên giữ nguyên: instructions, wSOL, PDAs, data, keys, ...
    instructions = []

    # --- Compute Budget ---
    instructions.append(set_compute_unit_price(1_000))
    instructions.append(set_compute_unit_limit(250_000))

    # --- Normalize pubkeys ---
    payer_pubkey = Pubkey.from_string(str(payer_pubkey)) if not isinstance(payer_pubkey, Pubkey) else payer_pubkey
    position_nft_owner = Pubkey.from_string(str(position_nft_owner)) if not isinstance(position_nft_owner, Pubkey) else position_nft_owner
    pool_state = Pubkey.from_string(str(pool_state)) if not isinstance(pool_state, Pubkey) else pool_state
    vault_0_mint = Pubkey.from_string(str(params["token0_mint"]))
    vault_1_mint = Pubkey.from_string(str(params["token1_mint"]))
    pool_vault_0 = Pubkey.from_string(str(params["token_vault_0"]))
    pool_vault_1 = Pubkey.from_string(str(params["token_vault_1"]))

    token0_program = detect_token_program(client, vault_0_mint)
    token1_program = detect_token_program(client, vault_1_mint)
    print(f"- Token0 Program: {token0_program}")
    print(f"- Token1 Program: {token1_program}")
    
    # --- User token ATA ---
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint, token0_program)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint, token1_program)

    # --- Handle WSOL wrapping ---
    temp_wsol_account = None
    wrapped_sol_mint_str = str(WRAPPED_SOL_MINT)
    
    if str(vault_0_mint) == wrapped_sol_mint_str or str(vault_1_mint) == wrapped_sol_mint_str:
        wsol_amount = amount_0_max if str(vault_0_mint) == wrapped_sol_mint_str else amount_1_max
        
        # Generate unique seed for WSOL account
        seed = f"wsol_{int(time.time() * 1000)}"
        
        # Derive WSOL account address from seed
        temp_wsol_account = Pubkey.create_with_seed(payer_pubkey, seed, TOKEN_PROGRAM_ID)
        
        # 1. Rent exemption
        rent_lamports = client.get_minimum_balance_for_rent_exemption(165).value
        
        # 1) Create temp WSOL account with seed
        instructions.append(
            create_account_with_seed(
                CreateAccountWithSeedParams(
                    from_pubkey=payer_pubkey,
                    to_pubkey=temp_wsol_account,
                    base=payer_pubkey,
                    seed=seed,
                    lamports=rent_lamports + wsol_amount,
                    space=165,
                    owner=TOKEN_PROGRAM_ID,
                )
            )
        )
        
        # 2) Initialize as WSOL token account
        instructions.append(
            initialize_account(
                InitializeAccountParams(
                    account=temp_wsol_account,
                    mint=WRAPPED_SOL_MINT,
                    owner=payer_pubkey,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # --- Determine source accounts for tokens ---
    source_token0 = temp_wsol_account if str(vault_0_mint) == wrapped_sol_mint_str else ata_token0
    source_token1 = temp_wsol_account if str(vault_1_mint) == wrapped_sol_mint_str else ata_token1

    # --- Position NFT mint keypair (MUST BE SIGNER per IDL) ---
    position_nft_mint_kp = Keypair()
    position_nft_mint_pubkey = position_nft_mint_kp.pubkey()
    
    # --- Derive PDAs ---
    personal_position, _ = Pubkey.find_program_address(
        [b"position", bytes(position_nft_mint_pubkey)], 
        PANCAKE_PROGRAM_ID
    )
    position_nft_account_pubkey = get_associated_token_address(
        position_nft_owner, 
        position_nft_mint_pubkey, 
        token_program_id=TOKEN_2022_PROGRAM_ID
    )

    # --- Build open_position instruction data ---
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)
    
    # tick_lower_index: i32
    data += struct.pack("<i", int(params["tick_lower_index"]))
    # tick_upper_index: i32
    data += struct.pack("<i", int(params["tick_upper_index"]))
    # tick_array_lower_start_index: i32
    data += struct.pack("<i", int(params["tick_array_lower_start_index"]))
    # tick_array_upper_start_index: i32
    data += struct.pack("<i", int(params["tick_array_upper_start_index"]))
    
    # liquidity: u128 (16 bytes, little-endian)
    liquidity_low = liquidity & ((1 << 64) - 1)
    liquidity_high = (liquidity >> 64) & ((1 << 64) - 1)
    data += struct.pack("<Q", liquidity_low)
    data += struct.pack("<Q", liquidity_high)
    
    # amount_0_max: u64
    data += struct.pack("<Q", amount_0_max)
    # amount_1_max: u64
    data += struct.pack("<Q", amount_1_max)
    
    # with_metadata: bool
    data += struct.pack("<?", with_metadata)
    
    # base_flag: Option<bool>
    if base_flag is None:
        data += struct.pack("<?", False)  # None variant
    else:
        data += struct.pack("<?", True)   # Some variant
        data += struct.pack("<?", bool(base_flag))

    # --- Build accounts list (MUST MATCH IDL ORDER) ---
    keys = [
        # 0. payer
        AccountMeta(payer_pubkey, is_signer=True, is_writable=True),
        # 1. position_nft_owner
        AccountMeta(position_nft_owner, is_signer=True, is_writable=False),
        # 2. position_nft_mint (MUST BE SIGNER per IDL!)
        AccountMeta(position_nft_mint_pubkey, is_signer=True, is_writable=True),
        # 3. position_nft_account
        AccountMeta(position_nft_account_pubkey, is_signer=False, is_writable=True),
        # 4. pool_state
        AccountMeta(pool_state, is_signer=False, is_writable=True),
        # 5. protocol_position
        AccountMeta(Pubkey.from_string(params["protocol_position"]), is_signer=False, is_writable=True),
        # 6. tick_array_lower
        AccountMeta(Pubkey.from_string(params["tick_array_lower"]), is_signer=False, is_writable=True),
        # 7. tick_array_upper
        AccountMeta(Pubkey.from_string(params["tick_array_upper"]), is_signer=False, is_writable=True),
        # 8. personal_position
        AccountMeta(personal_position, is_signer=False, is_writable=True),
        # 9. token_account_0 (user's source for token0)
        AccountMeta(source_token0, is_signer=False, is_writable=True),
        # 10. token_account_1 (user's source for token1)
        AccountMeta(source_token1, is_signer=False, is_writable=True),
        # 11. token_vault_0
        AccountMeta(pool_vault_0, is_signer=False, is_writable=True),
        # 12. token_vault_1
        AccountMeta(pool_vault_1, is_signer=False, is_writable=True),
        # 13. rent
        AccountMeta(RENT, is_signer=False, is_writable=False),
        # 14. system_program
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        # 15. token_program
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        # 16. associated_token_program
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        # 17. token_program_2022
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        # 18. vault_0_mint
        AccountMeta(vault_0_mint, is_signer=False, is_writable=False),
        # 19. vault_1_mint
        AccountMeta(vault_1_mint, is_signer=False, is_writable=False),
    ]
    
    tick_array_bitmap_extension_account = get_tick_array_bitmap_account_from_db(pool_id=str(pool_state), tick_lower=params["tick_lower_index"], tick_upper=params["tick_upper_index"])
    if tick_array_bitmap_extension_account is not None:
        keys.append(AccountMeta(Pubkey.from_string(tick_array_bitmap_extension_account), is_signer=False, is_writable=False))

    # Add open_position instruction
    open_pos_ix = Instruction(PANCAKE_PROGRAM_ID, bytes(data), keys)
    instructions.append(open_pos_ix)
    
    # --- Close temp WSOL account if created ---
    if temp_wsol_account is not None:
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=temp_wsol_account,
                    dest=payer_pubkey,  # Return rent to owner
                    owner=payer_pubkey,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # === Compile MessageV0 ===
    blockhash_resp = client.get_latest_blockhash()
    recent_blockhash = (
        blockhash_resp.value.blockhash
        if hasattr(blockhash_resp, "value")
        else blockhash_resp["result"]["value"]["blockhash"]
    )

    # chuẩn hóa kiểu Hash nếu cần (tùy client của bạn)
    # nếu dùng solders.hash.Hash:
    # from solders.hash import Hash
    # recent_blockhash = Hash.from_string(recent_blockhash)

    msg = MessageV0.try_compile(
        payer=payer_pubkey,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )

    # === Tạo VersionedTransaction & partial sign ===
    #
    # Giả thiết: payer_pubkey == position_nft_owner (như bạn nói)
    # Trong message sẽ có 2 signer unique:
    #   1) payer (cũng là owner)
    #   2) position_nft_mint_pubkey
    #
    # Ta ký NFT mint trên backend, còn payer dùng NullSigner
    signers = (
        NullSigner(payer_pubkey),     # chỗ trống để Phantom ký sau
        position_nft_mint_kp,         # backend ký real cho NFT mint
    )

    tx = VersionedTransaction(msg, signers)  # solders sẽ tạo chữ ký tương ứng:contentReference[oaicite:0]{index=0}

    # (OPTIONAL) Bạn có thể simulate ở backend nếu muốn, bằng solders.rpc.requests
    # from solders.rpc.requests import SimulateVersionedTransaction
    # from solders.rpc.config import RpcSimulateTransactionConfig
    # from solders.commitment_config import CommitmentLevel
    #
    # sim_cfg = RpcSimulateTransactionConfig(
    #     sig_verify=False,
    #     commitment=CommitmentLevel.Processed,
    # )
    # sim_req = SimulateVersionedTransaction(tx, sim_cfg)
    # raw = client._provider.make_request(sim_req)  # tùy client; có thể khác chỗ này
    # ... tự bạn check raw["result"]["value"]["err"]

    tx_bytes = bytes(tx)
    tx_base64 = b64encode(tx_bytes).decode("utf-8")

    print("=== DEBUG INFO (backend) ===")
    print(f"Number of instructions: {len(instructions)}")
    print(f"Position NFT mint: {position_nft_mint_pubkey}")
    print(f"Tx size (bytes): {len(tx_bytes)}")

    return {
        "tx_base64": tx_base64,  # transaction đã được ký bởi NFT mint, payer để NullSigner
        "position_nft_mint": str(position_nft_mint_pubkey),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "recent_blockhash": str(recent_blockhash),
    }


def build_mint_position_tx_sol_v10(
    client,
    payer_pubkey,
    position_nft_owner,
    pool_state,
    params: dict,
    amount_0_max: int,
    amount_1_max: int,
    liquidity: int = 0,
    with_metadata: bool = True,
    base_flag: bool | None = True
):
    """
    BUILD + PRESIGN (position_nft_mint) transaction
    Payer sẽ được Phantom ký ở frontend
    """

    instructions = []

    # --- Compute budget ---
    instructions.append(set_compute_unit_price(2500))
    instructions.append(set_compute_unit_limit(600_000))

    # --- Normalize keys ---
    def P(x): return Pubkey.from_string(str(x)) if not isinstance(x, Pubkey) else x

    payer_pubkey = P(payer_pubkey)
    position_nft_owner = P(position_nft_owner)
    pool_state = P(pool_state)

    vault_0_mint = P(params["token0_mint"])
    vault_1_mint = P(params["token1_mint"])
    pool_vault_0 = P(params["token_vault_0"])
    pool_vault_1 = P(params["token_vault_1"])

    # --- User ATAs ---
    ata_token0 = get_associated_token_address(position_nft_owner, vault_0_mint)
    ata_token1 = get_associated_token_address(position_nft_owner, vault_1_mint)

    # --- WSOL handling (optional) ---
    temp_wsol_account = None
    wrapped_mint_str = str(WRAPPED_SOL_MINT)

    if str(vault_0_mint) == wrapped_mint_str or str(vault_1_mint) == wrapped_mint_str:
        wsol_amount = amount_0_max if str(vault_0_mint) == wrapped_mint_str else amount_1_max

        seed = f"wsol_{int(time.time()*1000)}"
        temp_wsol_account = Pubkey.create_with_seed(payer_pubkey, seed, TOKEN_PROGRAM_ID)

        rent_resp = client.get_minimum_balance_for_rent_exemption(165)
        rent_lamports = rent_resp.value if hasattr(rent_resp, "value") else rent_resp["result"]["value"]

        instructions.append(
            create_account_with_seed(
                CreateAccountWithSeedParams(
                    from_pubkey=payer_pubkey,
                    to_pubkey=temp_wsol_account,
                    base=payer_pubkey,
                    seed=seed,
                    lamports=rent_lamports + wsol_amount,
                    space=165,
                    owner=TOKEN_PROGRAM_ID
                )
            )
        )

        instructions.append(
            initialize_account(
                InitializeAccountParams(
                    account=temp_wsol_account,
                    mint=WRAPPED_SOL_MINT,
                    owner=payer_pubkey,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # --- token sources ---
    source_token0 = temp_wsol_account if str(vault_0_mint) == wrapped_mint_str else ata_token0
    source_token1 = temp_wsol_account if str(vault_1_mint) == wrapped_mint_str else ata_token1

    # --- Position NFT mint (SIGNER) ---
    position_nft_mint_kp = Keypair()
    position_nft_mint_pubkey = position_nft_mint_kp.pubkey()

    # --- PDAs ---
    personal_position, _ = Pubkey.find_program_address(
        [b"position", bytes(position_nft_mint_pubkey)],
        PANCAKE_PROGRAM_ID
    )

    position_nft_account_pubkey = get_associated_token_address(
        position_nft_owner,
        position_nft_mint_pubkey,
        token_program_id=TOKEN_2022_PROGRAM_ID
    )

    # --- Instruction data ---
    discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
    data = bytearray(discriminator)

    data += struct.pack("<i", int(params["tick_lower_index"]))
    data += struct.pack("<i", int(params["tick_upper_index"]))
    data += struct.pack("<i", int(params["tick_array_lower_start_index"]))
    data += struct.pack("<i", int(params["tick_array_upper_start_index"]))

    liquidity_low = liquidity & ((1 << 64) - 1)
    liquidity_high = (liquidity >> 64) & ((1 << 64) - 1)
    data += struct.pack("<Q", liquidity_low)
    data += struct.pack("<Q", liquidity_high)

    data += struct.pack("<Q", amount_0_max)
    data += struct.pack("<Q", amount_1_max)

    data += struct.pack("<?", with_metadata)

    if base_flag is None:
        data += struct.pack("<?", False)
    else:
        data += struct.pack("<?", True)
        data += struct.pack("<?", bool(base_flag))

    # --- Accounts (exact IDL order) ---
    keys = [
        AccountMeta(payer_pubkey, is_signer=True, is_writable=True),
        AccountMeta(position_nft_owner, is_signer=False, is_writable=False),
        AccountMeta(position_nft_mint_pubkey, is_signer=True, is_writable=True),
        AccountMeta(position_nft_account_pubkey, is_signer=False, is_writable=True),
        AccountMeta(pool_state, is_signer=False, is_writable=True),
        AccountMeta(P(params["protocol_position"]), is_signer=False, is_writable=True),
        AccountMeta(P(params["tick_array_lower"]), is_signer=False, is_writable=True),
        AccountMeta(P(params["tick_array_upper"]), is_signer=False, is_writable=True),
        AccountMeta(personal_position, is_signer=False, is_writable=True),
        AccountMeta(source_token0, is_signer=False, is_writable=True),
        AccountMeta(source_token1, is_signer=False, is_writable=True),
        AccountMeta(pool_vault_0, is_signer=False, is_writable=True),
        AccountMeta(pool_vault_1, is_signer=False, is_writable=True),
        AccountMeta(RENT, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(vault_0_mint, is_signer=False, is_writable=False),
        AccountMeta(vault_1_mint, is_signer=False, is_writable=False)
    ]

    bitmap_acc = get_tick_array_bitmap_account_from_db(
        pool_id=str(pool_state),
        tick_lower=params["tick_lower_index"],
        tick_upper=params["tick_upper_index"]
    )
    if bitmap_acc:
        keys.append(AccountMeta(P(bitmap_acc), is_signer=False, is_writable=False))

    # --- Instruction ---
    ix = Instruction(PANCAKE_PROGRAM_ID, bytes(data), keys)
    instructions.append(ix)

    # --- Close WSOL ---
    if temp_wsol_account:
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=temp_wsol_account,
                    dest=position_nft_owner,  # Hoàn lại rent cho owner
                    owner=position_nft_owner,
                    program_id=TOKEN_PROGRAM_ID,
                )
            )
        )

    # --- Blockhash ---
    blockhash_resp = client.get_latest_blockhash()
    recent_blockhash = blockhash_resp.value.blockhash if hasattr(blockhash_resp, "value") else blockhash_resp["result"]["value"]["blockhash"]

    # --- Compile MessageV0 ---
    msg = MessageV0.try_compile(
        payer_pubkey,
        instructions,
        [],
        recent_blockhash
    )

    # === PRE-SIGN MINT TRÊN MESSAGE, KHÔNG TẠO TX Ở BACKEND ===

    # 1) signer slots (theo thứ tự is_signer)
    signer_account_indexes = [i for i, _k in enumerate(msg.account_keys) if msg.is_signer(i)]
    # ví dụ: [0, 2] hoặc [0, 1] tùy bạn set is_signer ở trên

    # 2) tìm account index của mint
    mint_account_index = None
    for i, k in enumerate(msg.account_keys):
        if k == position_nft_mint_pubkey:
            mint_account_index = i
            break

    if mint_account_index is None:
        raise Exception("position_nft_mint_pubkey not found in msg.account_keys")

    # 3) map về signer index
    mint_signer_index = signer_account_indexes.index(mint_account_index)

    # 4) ký message bytes bằng mint keypair
    msg_bytes = bytes(msg)
    mint_signature = position_nft_mint_kp.sign_message(msg_bytes)
    mint_signature_base64 = b64encode(bytes(mint_signature)).decode("utf-8")

    # 5) encode message base64 để gửi frontend
    msg_base64 = b64encode(msg_bytes).decode("utf-8")

    print("=== DEBUG – Mint Position V12 ===")
    print("Required_signatures:", msg.header.num_required_signatures)
    print("Signer_account_indexes:", signer_account_indexes)
    print("Mint_account_index:", mint_account_index)
    print("Mint_signer_index:", mint_signer_index)
    print("Payer:", payer_pubkey)
    print("Mint:", position_nft_mint_pubkey)

    return {
        "msg_base64": msg_base64,
        "mint_signature": mint_signature_base64,
        "mint_signer_index": mint_signer_index,
        "payer_pubkey": str(payer_pubkey),
        "position_nft_mint": str(position_nft_mint_pubkey),
        "position_nft_account": str(position_nft_account_pubkey),
        "personal_position": str(personal_position),
        "recent_blockhash": str(recent_blockhash),
    }