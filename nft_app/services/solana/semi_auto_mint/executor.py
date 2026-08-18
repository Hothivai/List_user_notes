import struct
import time
import base64
import math
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.sysvar import RENT
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.null_signer import NullSigner
from solana.rpc.api import Client
from solders.system_program import create_account, CreateAccountParams, CreateAccountWithSeedParams, create_account_with_seed
from spl.token.constants import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID, WRAPPED_SOL_MINT
from spl.token.instructions import get_associated_token_address, initialize_account, InitializeAccountParams, close_account, CloseAccountParams

from services.liquidity_actions.helper import get_tick_array_bitmap_account_from_db

# --- CONSTANTS ---
# Program ID PancakeSwap V3 trên Solana Mainnet
PANCAKE_V3_PROGRAM_ID = Pubkey.from_string("HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq")
TICK_ARRAY_SIZE = 60

class PositionExecutor:
    def __init__(self, rpc_client: Client):
        self.client = rpc_client

    # =================== HELPER METHODS ===================
    
    def detect_token_program(self, mint: Pubkey) -> Pubkey:
        """Kiểm tra xem Token Mint thuộc chuẩn cũ hay Token-2022"""
        if str(mint) == str(WRAPPED_SOL_MINT):
            return TOKEN_PROGRAM_ID
            
        try:
            resp = self.client.get_account_info(mint)
            if resp.value is None:
                # Nếu không tìm thấy, raise hoặc default. Ở đây default về TOKEN_PROGRAM_ID cho an toàn
                return TOKEN_PROGRAM_ID
            
            owner = resp.value.owner
            if owner == TOKEN_PROGRAM_ID:
                return TOKEN_PROGRAM_ID
            elif owner == TOKEN_2022_PROGRAM_ID:
                return TOKEN_2022_PROGRAM_ID
            else:
                return TOKEN_PROGRAM_ID 
        except Exception:
            return TOKEN_PROGRAM_ID

    def get_start_tick_index(self, tick_index: int, tick_spacing: int) -> int:
        return (tick_index // (TICK_ARRAY_SIZE * tick_spacing)) * (TICK_ARRAY_SIZE * tick_spacing)

    def derive_tick_array_pda(self, pool_pubkey: Pubkey, start_tick_index: int) -> Pubkey:
        seed_index_bytes = start_tick_index.to_bytes(4, "big", signed=True)
        seeds = [b"tick_array", bytes(pool_pubkey), seed_index_bytes]
        pda, _ = Pubkey.find_program_address(seeds, PANCAKE_V3_PROGRAM_ID)
        return pda

    def derive_protocol_position(self, pool_pubkey: Pubkey, tick_lower: int, tick_upper: int) -> Pubkey:
        t_lower = tick_lower.to_bytes(4, "big", signed=True)
        t_upper = tick_upper.to_bytes(4, "big", signed=True)
        seeds = [b"position", bytes(pool_pubkey), t_lower, t_upper]
        pda, _ = Pubkey.find_program_address(seeds, PANCAKE_V3_PROGRAM_ID)
        return pda

    def derive_personal_position(self, nft_mint: Pubkey) -> Pubkey:
        seeds = [b"position", bytes(nft_mint)]
        pda, _ = Pubkey.find_program_address(seeds, PANCAKE_V3_PROGRAM_ID)
        return pda

    # =================== MAIN BUILDER ===================

    def build_mint_transaction(self, 
                               payer_address: str,
                               pool_address: str,
                               token0_mint: str,
                               token1_mint: str,
                               token_vault_0: str,
                               token_vault_1: str,
                               tick_lower_index: int,
                               tick_upper_index: int,
                               tick_spacing: int,
                               liquidity: int,
                               amount0_max: int,
                               amount1_max: int,
                               bitmap_extension_address: str = None):
        """
        Xây dựng Transaction Mint Position hoàn chỉnh (đã bao gồm Logic WSOL).
        Trả về base64 transaction đã được ký bởi NFT Mint Keypair.
        """
        
        # 1. Chuẩn hóa Pubkeys
        payer_pubkey = Pubkey.from_string(payer_address)
        pool_pubkey = Pubkey.from_string(pool_address)
        mint0_pubkey = Pubkey.from_string(token0_mint)
        mint1_pubkey = Pubkey.from_string(token1_mint)
        vault0_pubkey = Pubkey.from_string(token_vault_0)
        vault1_pubkey = Pubkey.from_string(token_vault_1)
        
        if bytes(mint0_pubkey) > bytes(mint1_pubkey):
            print(f"⚠️ Sorting Required: {mint0_pubkey} > {mint1_pubkey}. Swapping 0 <-> 1.")
            mint0, mint1 = mint1_pubkey, mint0_pubkey
            vault0, vault1 = vault1_pubkey, vault0_pubkey
            amount0_max, amount1_max = amount1_max, amount0_max
        else:
            mint0, mint1 = mint0_pubkey, mint1_pubkey
            vault0, vault1 = vault0_pubkey, vault1_pubkey

        instructions = []

        # 2. Compute Budget (Tăng phí để dễ khớp lệnh trên Mainnet)
        instructions.append(set_compute_unit_price(1_000)) 
        instructions.append(set_compute_unit_limit(250_000))

        # 3. Detect Token Programs (Hỗ trợ Token-2022)
        token0_program = self.detect_token_program(mint0_pubkey)
        token1_program = self.detect_token_program(mint1_pubkey)

        # 4. Xác định Token Source (ATA)
        ata_token0 = get_associated_token_address(payer_pubkey, mint0_pubkey, token0_program)
        ata_token1 = get_associated_token_address(payer_pubkey, mint1_pubkey, token1_program)

        # 5. Xử lý WSOL (Wrapped SOL)
        # Nếu Token 0 hoặc Token 1 là Native SOL, ta cần tạo ví tạm thời
        temp_wsol_account = None
        wrapped_sol_mint_str = str(WRAPPED_SOL_MINT)
        
        if str(mint0_pubkey) == wrapped_sol_mint_str or str(mint1_pubkey) == wrapped_sol_mint_str:
            wsol_amount = amount0_max if str(mint0_pubkey) == wrapped_sol_mint_str else amount1_max
            
            # Tạo seed unique dựa trên timestamp
            seed = f"wsol_{int(time.time() * 1000)}"
            temp_wsol_account = Pubkey.create_with_seed(payer_pubkey, seed, TOKEN_PROGRAM_ID)
            
            rent_lamports = self.client.get_minimum_balance_for_rent_exemption(165).value
            
            # Instruction tạo account WSOL tạm
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
            # Init account
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

        # Xác định lại Source Account cuối cùng (Dùng WSOL tạm hay ATA thật)
        source_token0 = temp_wsol_account if str(mint0_pubkey) == wrapped_sol_mint_str else ata_token0
        source_token1 = temp_wsol_account if str(mint1_pubkey) == wrapped_sol_mint_str else ata_token1

        # 6. Chuẩn bị Position NFT (Keypair mới - Backend ký)
        position_nft_mint_kp = Keypair()
        position_nft_mint_pubkey = position_nft_mint_kp.pubkey()

        # 7. Tính toán các PDA cần thiết
        personal_position_pda = self.derive_personal_position(position_nft_mint_pubkey)
        protocol_position_pda = self.derive_protocol_position(pool_pubkey, tick_lower_index, tick_upper_index)
        
        tick_array_lower_start = self.get_start_tick_index(tick_lower_index, tick_spacing)
        tick_array_upper_start = self.get_start_tick_index(tick_upper_index, tick_spacing)
        
        tick_array_lower_pda = self.derive_tick_array_pda(pool_pubkey, tick_array_lower_start)
        tick_array_upper_pda = self.derive_tick_array_pda(pool_pubkey, tick_array_upper_start)
        
        # ATA chứa NFT Position (Luôn dùng Token-2022 cho NFT V3)
        position_nft_account_pubkey = get_associated_token_address(
            payer_pubkey, 
            position_nft_mint_pubkey, 
            token_program_id=TOKEN_2022_PROGRAM_ID
        )

        # 8. Build Instruction Data (Manual Packing - Giữ nguyên logic cũ)
        # Discriminator cho `open_position_with_token22_nft`
        discriminator = bytes([77, 255, 174, 82, 125, 29, 201, 46])
        data = bytearray(discriminator)
        
        data += struct.pack("<i", int(tick_lower_index))
        data += struct.pack("<i", int(tick_upper_index))
        data += struct.pack("<i", int(tick_array_lower_start))
        data += struct.pack("<i", int(tick_array_upper_start))
        
        # Liquidity (u128 split thành 2 u64 little-endian)
        liquidity_low = int(liquidity) & ((1 << 64) - 1)
        liquidity_high = (int(liquidity) >> 64) & ((1 << 64) - 1)
        data += struct.pack("<Q", liquidity_low)
        data += struct.pack("<Q", liquidity_high)
        
        data += struct.pack("<Q", int(amount0_max))
        data += struct.pack("<Q", int(amount1_max))
        data += struct.pack("<?", True)  # with_metadata
        data += struct.pack("<?", True)  # base_flag Some
        data += struct.pack("<?", True)  # base_flag True (fix_amount)

        # 9. Build Account Meta List (Thứ tự cực kỳ quan trọng - theo IDL)
        keys = [
            AccountMeta(payer_pubkey, is_signer=True, is_writable=True),            # 0. payer
            AccountMeta(payer_pubkey, is_signer=True, is_writable=False),           # 1. position_nft_owner
            AccountMeta(position_nft_mint_pubkey, is_signer=True, is_writable=True), # 2. nft_mint (Signer)
            AccountMeta(position_nft_account_pubkey, is_signer=False, is_writable=True), # 3. nft_account
            AccountMeta(pool_pubkey, is_signer=False, is_writable=True),            # 4. pool_state
            AccountMeta(protocol_position_pda, is_signer=False, is_writable=True),  # 5. protocol_pos
            AccountMeta(tick_array_lower_pda, is_signer=False, is_writable=True),   # 6. tick_lower
            AccountMeta(tick_array_upper_pda, is_signer=False, is_writable=True),   # 7. tick_upper
            AccountMeta(personal_position_pda, is_signer=False, is_writable=True),  # 8. personal_pos
            AccountMeta(source_token0, is_signer=False, is_writable=True),          # 9. token_acc_0
            AccountMeta(source_token1, is_signer=False, is_writable=True),          # 10. token_acc_1
            AccountMeta(vault0_pubkey, is_signer=False, is_writable=True),          # 11. vault_0
            AccountMeta(vault1_pubkey, is_signer=False, is_writable=True),          # 12. vault_1
            AccountMeta(RENT, is_signer=False, is_writable=False),                  # 13. rent
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),     # 14. system
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),      # 15. token_prog
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False), # 16. ata_prog
            AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False), # 17. token22_prog
            AccountMeta(mint0_pubkey, is_signer=False, is_writable=False),          # 18. mint_0
            AccountMeta(mint1_pubkey, is_signer=False, is_writable=False),          # 19. mint_1
        ]
        
        bitmap_extension_address = get_tick_array_bitmap_account_from_db(pool_id=str(pool_address), tick_lower=tick_lower_index, tick_upper=tick_upper_index)
        
        # Nếu có Bitmap Extension (cho full range hoặc special pools), thêm vào cuối
        if bitmap_extension_address:
            keys.append(AccountMeta(Pubkey.from_string(bitmap_extension_address), is_signer=False, is_writable=False))

        # 10. Tạo Instruction Open Position
        open_pos_ix = Instruction(PANCAKE_V3_PROGRAM_ID, bytes(data), keys)
        instructions.append(open_pos_ix)

        # 11. Close WSOL Account (nếu đã tạo) -> Trả lại SOL thừa cho User
        if temp_wsol_account:
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

        # 12. Compile Transaction
        blockhash_resp = self.client.get_latest_blockhash()
        recent_blockhash = blockhash_resp.value.blockhash

        msg = MessageV0.try_compile(
            payer=payer_pubkey,
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )

        # 13. Partial Sign (Backend ký bằng NFT Mint Keypair)
        # Payer (User) để NullSigner để Client ký sau
        signers = (
            NullSigner(payer_pubkey),
            position_nft_mint_kp
        )
        
        tx = VersionedTransaction(msg, signers)

        # 14. Serialize ra Base64
        tx_bytes = bytes(tx)
        tx_base64 = base64.b64encode(tx_bytes).decode("utf-8")

        return {
            "tx_base64": tx_base64,
            "nft_mint_address": str(position_nft_mint_pubkey),
            "personal_position_address": str(personal_position_pda),
            "liquidity_minted": liquidity,
            "note": "Transaction partially signed. User needs to sign."
        }