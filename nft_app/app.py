
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from functools import wraps
from services.list_farm_pancake import get_nft_data
from services.aerodrome_dex.list_positions_aerodrome import get_aerodrome_nft_data, get_aerodrome_nft_data_all_npms

from services.execute_data import (
    insert_nft_data, fetch_list_bond_data, insert_bond_data, update_bond_status, delete_bond_contract, 
    update_bond_data, fetch_bond_data_by_contract_address, fetch_and_update_bonds, get_connection, 
    update_bond_threshold
)
from services.update_query import (
    fetch_all_pool_info,
    fetch_all_pool_sol_info,
    fetch_all_pool_info_aerodrome_db,
    fetch_latest_nft_id, fetch_latest_nft_by_wallet, fetch_latest_nft_by_wallet_and_chain,
    fetch_nft_history_by_id, count_nft_history_records_by_id, toggle_blacklist, fetch_blacklist_nft_ids,
    fetch_latest_summary_by_token, fetch_latest_summary_by_wallet_and_chain, get_latest_total_pending_cake_by_wallet,
    get_latest_total_pending_cake_by_wallet_and_chain, fetch_all_pool_info, fetch_all_pool_sol_info, enrich_with_pool_info, filter_by_token,
    get_futures_positions_binance_data_by_wallet, get_futures_orders_binance_data_by_wallet, fetch_all_pool_info_aerodrome_db, 
    toggle_stake_track_api_aerodrome, get_total_aero_per_day_each_chain, process_nft_summary, backfill_summary, process_batch_nft_summary,
    toggle_copy_bot_api, LATEST_POSITION_IDENTITY_JOIN
)

from services.transaction_history.tx_his import get_transactions_with_filter
from services.transaction_history.sol_tx_his import get_transaction_sol_with_filter
from services.solana.get_price_range_pool import analyze_pool_ticks
from services.excute_transaction import get_existing_wallet, get_transaction
from services.excute_transaction_v2 import get_transaction_v2, get_existing_wallet_v2, search_tx_by_nft_id
from services.transaction_history_v2.tx_his_v2 import get_transactions_with_filter as get_transactions_with_filter_v2
from services.transaction_history_v2.sol_tx_his_v2 import get_transaction_sol_with_filter as get_transaction_sol_with_filter_v2

from services.liquidity_actions.mint_position import get_data_mint
from services.liquidity_actions.mint_position_sol import get_data_mint_sol, get_mint_params_sol, build_mint_position_tx_sol_v8, build_mint_position_tx_sol_v9
from services.binance_apis.trade_funcs import sign_request, get_futures_positions, get_futures_orders, get_spot_account

from services.liquidity_actions.mint_position import *
from services.helper import merge_summary, validate_pool_backend

from services.configured_rebalancer_monitor import (
    enrich_nfts_with_configured_rebalancer,
    enrich_pools_with_configured_rebalancer,
    get_configured_rebalancer_context,
)

import os, secrets
from eth_account.messages import encode_defunct
from eth_account import Account
from web3 import Web3
from datetime import datetime, timezone, timedelta
import json
import time
from solders.pubkey import Pubkey
from config import *
from services.solana.get_wallet_info import * 
from logging_setup import system_logger as log
import base58
import nacl.signing
import nacl.exceptions
from services.solana.refresh_nft_data import process_nft, process_nft_evm, process_nft_evm_aerodrome

from services.solana.semi_auto_mint.mint_service import MintingService
from services.evm.semi_auto_mint.mint_service import V3ApiAggregator
from services.evm.collect_harvest.collect_harvest_service import CollectHarvestService

from apscheduler.schedulers.background import BackgroundScheduler
import subprocess
import socket

UTC_PLUS_7 = timezone(timedelta(hours=7))

app = Flask(__name__)

from routes.token_groups import token_groups_bp
app.register_blueprint(token_groups_bp)

# Thông tin đăng nhập cơ bản
USERNAME = "admin"
PASSWORD = "it.d@2025"  # Thay bằng mật khẩu mạnh của bạn

# Kiểm tra xác thực Basic Auth
def check_auth(username, password):
    return username == USERNAME and password == PASSWORD

# Trả về response yêu cầu đăng nhập
def authenticate():
    return Response(
        "You must be logged in.", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )

# Middleware yêu cầu đăng nhập trước mỗi request
@app.before_request
def require_auth():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()

app.secret_key = 'nhat12398'

@app.route("/api/me", methods=["GET"])
def me():
    user = session.get("user")
    if not user:
        return jsonify(logged_in=False)
    return jsonify(logged_in=True, address=user)

# API lấy nonce
@app.route("/api/get_nonce", methods=["GET"])
def get_nonce():
    nonce = secrets.token_hex(16)  # chuỗi random
    session["login_nonce"] = nonce
    return jsonify(nonce=nonce)

@app.route("/api/verify_signature", methods=["POST"])
def verify_signature():
    data = request.json
    address = data.get("address")
    signature = data.get("signature")
    chain = data.get("chain", "evm")  # mặc định là EVM
    nonce = session.get("login_nonce")

    if not nonce:
        return jsonify(error="NO_NONCE"), 400

    try:
        # ============ EVM (Ethereum, BSC, Polygon, ...) ============
        if chain.lower() == "evm":
            message = encode_defunct(text=nonce)
            recovered = Account.recover_message(message, signature=signature)
            if recovered.lower() == address.lower():
                session["user"] = address
                return jsonify(success=True, address=address, chain="evm")
            else:
                return jsonify(success=False, error="SIGNATURE_INVALID"), 401

        # ============ SOLANA ============
        elif chain.lower() == "solana":
            # ✅ Solana ký bằng Ed25519 → verify bằng PyNaCl
            public_key_bytes = base58.b58decode(address)
            signature_bytes = bytes.fromhex(signature)
            message_bytes = nonce.encode("utf-8")

            verify_key = nacl.signing.VerifyKey(public_key_bytes)
            try:
                verify_key.verify(message_bytes, signature_bytes)
                session["user"] = address
                return jsonify(success=True, address=address, chain="solana")
            except nacl.exceptions.BadSignatureError:
                return jsonify(success=False, error="SIGNATURE_INVALID"), 401

        else:
            return jsonify(error="UNSUPPORTED_CHAIN"), 400

    except Exception as e:
        return jsonify(error=str(e)), 400

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()  # Xóa tất cả dữ liệu session
    return jsonify({"success": True})

# Hàm định dạng theo token symbol
def format_token_amount(value, token_symbol):
    token = token_symbol.upper()
    if "ETH" in token or "BNB" in token:
        return f"{value:,.3f}"
    elif "BTC" in token:
        return f"{value:,.4f}"
    elif "SOL" in token:
        return f"{value:,.3f}"
    else:
        return f"{int(value):,}"

# Đăng ký cho Jinja2
app.jinja_env.globals.update(format_token_amount=format_token_amount)

last_update_time = None

def is_evm_address(wallet: str) -> bool:
    return wallet.startswith("0x")

def is_solana_address(wallet: str) -> bool:
    return not wallet.startswith("0x") 

wallet_list = [
    "0x88DE2AB47352779494547CaCCB31eE1A133dd334",
    "0x349F8F068120E04B359556E442A579Af41ebF486",
    "0x065994BeC6cA97AeF488f76824580814Be4E024F",
    "0x5b97C369E1931F70169839F44e846E4eCC29b05e",
    "0xafCf63AbF4d061fC000Ad1244c74851e52F67b01",
    "0x9b73E95909Be63F02b06130716384c3030C74D8D",
    "0x89B8274BbC46A0db474E3Df381688F80DfFccB6b",
    "0x0c9880AEcEDa007fD7967d1672D8C91b85e5c087",
    #"0xaE170F4479e44D81dEfA6D806Ad0AF32aF61117F",
    #"0x7A317d3C925EF0fd924592049325C0A0a840Af86",
    "CJoUCt78FNbJJcKW3CnmLG9CVq6ANSTiXWV1tyN5dXw9",
    "4rDyyA4vydw4T5uekxY5La4Ywv43nSZ2PgG7rfBfvQAJ",
    "DGHsf8b99KyWPErCbVuXcPUxAXwaC7bqndPgEVvmSAFn",
    "8x4zj74myKzox48jUMHskfNo4NHuAzXeLyXs7HLUSYzL"
]

def save_update_status(wallets, chains):
    update_data = {
        "last_update": datetime.now(timezone.utc).astimezone(UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": time.time(),
        "wallets": wallets,
        "chains": chains
    }
    with open("/app/nft_app/update_status.json", "w") as f:
    # with open("update_status.json", "w") as f:
        json.dump(update_data, f)

last_fetched_wallets = []
last_fetched_chains = []

def auto_fetch_data():
    global last_update_time, last_fetched_wallets, last_fetched_chains

    last_fetched_wallets = []
    last_fetched_chains = []
    errors = []
    
    # Get Aero Price USD
    aero_price_usd = get_aero_price_usd()

    for wallet in wallet_list:
        # --- 1. Xác định Chain ---
        chains = []
        if is_evm_address(wallet):
            chains = ["BNB", "ETH", "ARB", "BAS", "LIN", "MON"] # Cấu hình chain của bạn
        elif is_solana_address(wallet):
            try:
                Pubkey.from_string(wallet)
                chains = ["SOL"]
            except Exception:
                log.warning(f"⚠️ Wallet {wallet} invalid Sol address. Skip.")
                continue
        else:
            log.warning(f"⚠️ Wallet {wallet} invalid format. Skip.")
            continue

        # --- 2. Fetch Data Từng Chain ---
        for chain_name in chains:
            print(f"🔍 Getting data for {wallet} on chain {chain_name}")
            
            # Reset container cho mỗi chain
            nft_data = [] 
            
            # === KHỐI XỬ LÝ EVM (Pancake + Aerodrome) ===
            if chain_name != "SOL":
                cs_wallet = Web3.to_checksum_address(wallet)
                
                # [SAFE BLOCK 1] PancakeSwap
                try:
                    pancake_nft_data = get_nft_data(cs_wallet, chain_name)
                    if pancake_nft_data:
                        nft_data.extend(pancake_nft_data)
                    else:
                        # log.debug để đỡ spam log warning
                        log.debug(f"ℹ️ No Pancake NFT for {wallet} on {chain_name}") 
                except Exception as e:
                    log.error(f"⚠️ PancakeSwap fetch failed for {wallet} on {chain_name}: {e}")
                    errors.append((wallet, chain_name, f"Pancake: {e}"))

                # [SAFE BLOCK 2] Aerodrome (Chỉ chạy trên BAS)
                if chain_name == "BAS":
                    try:
                        aerodrome_nft_data = get_aerodrome_nft_data_all_npms(
                            cs_wallet,
                            AERODROME_NPM_ADDRESSES.get(chain_name, []),
                            chain_name,
                            aero_price_usd,
                        )
                        if aerodrome_nft_data:
                            nft_data.extend(aerodrome_nft_data)
                            log.info(f"   + Found {len(aerodrome_nft_data)} Aerodrome positions across configured NPMs")
                    except Exception as e:
                        log.error(f"âš ï¸ Aerodrome multi-NPM fetch failed for {wallet}: {e}")
                        errors.append((wallet, chain_name, f"Aerodrome: {e}"))
                    for npm_address in []:
                        try:
                            aerodrome_nft_data = get_aerodrome_nft_data(
                                cs_wallet, npm_address, chain_name, aero_price_usd
                            )
                            if aerodrome_nft_data:
                                nft_data.extend(aerodrome_nft_data)
                                log.info(f"   + Found {len(aerodrome_nft_data)} Aerodrome positions in NPM {npm_address[:8]}...")
                        except Exception as e:
                            log.error(f"⚠️ Aerodrome fetch failed for {wallet} on NPM {npm_address[:10]}: {e}")
                            errors.append((wallet, chain_name, f"Aerodrome ({npm_address[:6]}): {e}"))
            
            # === KHỐI XỬ LÝ SOLANA === #
            else:
                try:
                    wallet_pubkey = Pubkey.from_string(wallet)
                    sol_nft_data = get_nft_solana_data(wallet_pubkey, TOKEN_ACCOUNT_OPTS, chain_name)
                    if sol_nft_data:
                        nft_data.extend(sol_nft_data)
                except Exception as e:
                    log.error(f"⚠️ Solana fetch failed for {wallet}: {e}")
                    errors.append((wallet, chain_name, f"Solana: {e}"))

            # --- 3. Insert Database (Quan trọng) ---
            # Đưa ra khỏi try/except fetch để đảm bảo:
            # Dù 1 trong 2 nguồn lỗi, nguồn còn lại vẫn được lưu.
            try:
                if nft_data:
                    # insert_nft_data(nft_data)
                    # process_batch_nft_summary(nft_data)
                    new_ids = insert_nft_data(nft_data)
                    process_batch_nft_summary(nft_data, list_ids=new_ids)
                    
                    log.info(f"✅ Inserted {len(nft_data)} NFT(s) for {wallet} on {chain_name}")
                else:
                    log.warning(f"ℹ️ No NFT data found for {wallet} on {chain_name} (Total)")
                
                last_fetched_chains.append(chain_name)

            except Exception as e_db:
                log.error(f"❌ Database Insert Failed for {wallet} on {chain_name}: {e_db}")
                errors.append((wallet, chain_name, f"DB Error: {e_db}"))

            time.sleep(1) # Sleep nhẹ

        last_fetched_wallets.append(str(wallet))
        time.sleep(2)

    last_update_time = datetime.now()
    save_update_status(last_fetched_wallets, last_fetched_chains)
    
    # Update Backfill Summary
    # log.info("📦 Updating backfill summary...")
    # backfill_summary()

    # Summary
    log.info(f"🏁 Finished. Wallets: {len(last_fetched_wallets)} | Errors: {len(errors)}")

@app.route('/check_update')
def check_update():
    try:
        # Đọc dữ liệu từ file JSON
        with open("/app/nft_app/update_status.json") as f:
        # with open("update_status.json") as f:
            update_data = json.load(f)
            return jsonify({
                "status": "success",
                "timestamp": update_data.get("timestamp"),
                "last_update": update_data.get("last_update"),
                "wallets": update_data.get("wallets"),
                "chains": update_data.get("chains"),
                "message": f"✅ Fetched and saved data for wallet: {', '.join(update_data.get('wallets', []))} on chains: {', '.join(update_data.get('chains', []))}"
            })
    except Exception as e:
        return jsonify({"status": "no_update", "message": None})

@app.route('/', methods=['GET', 'POST'])
def index():
    wallet_address = None
    message = None
    start_date = None
    end_date = None
    nft_data = []
    summary_data = []

    page = int(request.args.get("page", 1))  # Lấy số trang từ query param
    per_page = 30  # Số item mỗi trang
    total_pages = 0
    total_pending_cake = 0
    
    token = request.args.get('token', '').strip()
    
    # New param để lấy lịch sử của 1 NFT cụ thể
    nft_id = request.args.get('nft_id')
    chain = request.args.get('chain')

    # Get Aero Price USD
    aero_price_usd = get_aero_price_usd()

    if request.method == 'POST':
        wallet_address = request.form.get('wallet_address', '').strip()
        chain_name = request.form.get('chain', '').strip()
        action = request.form.get('action')
        chain_name = request.form.get('chain')
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        
        # Binance API key
        # binance_api_key = request.form.get("binance_api_key")
        # binance_secret_key = request.form.get("binance_secret_key")
        
        # session["binance_api_key"] = binance_api_key
        # session["binance_secret_key"] = binance_secret_key

        if not wallet_address:
            session['message'] = "⚠️ Please enter a wallet address."
        else:
            try:
                if is_evm_address(wallet_address):
                    checksum_wallet = Web3.to_checksum_address(wallet_address)
                elif is_solana_address(wallet_address):
                    checksum_wallet = Pubkey.from_string(wallet_address)
                else:
                    session['message'] = "⚠️ Please enter a valid wallet address."
                    return redirect(url_for("index"))

                if action == 'fetch_and_store':
                    if chain_name == "SOL":
                        fetched_nft_data = get_nft_solana_data(checksum_wallet, TOKEN_ACCOUNT_OPTS, chain_name)
                    else:
                        fetch_data_time = int((datetime.now() - timedelta(hours=2)).timestamp()) 
                        fetched_nft_data = get_nft_data(checksum_wallet, chain_name, six_months_ago=fetch_data_time)
                        if chain_name == "BAS":
                            # aerodrome_nft_data = get_aerodrome_nft_data(checksum_wallet, AERODROME_NPM_ADDRESSES["BAS"], chain_name, aero_price_usd)
                            aerodrome_nft_data = get_aerodrome_nft_data_all_npms(checksum_wallet, AERODROME_NPM_ADDRESSES["BAS"], chain_name, aero_price_usd)
                            fetched_nft_data += aerodrome_nft_data
                        
                    if fetched_nft_data:
                        # insert_nft_data(fetched_nft_data)
                        # process_batch_nft_summary(fetched_nft_data)

                        new_ids = insert_nft_data(fetched_nft_data)
                        process_batch_nft_summary(fetched_nft_data, list_ids=new_ids)
                        
                        # Update Backfill Summary
                        # log.info(f"✅ Update backfill summary...")
                        # backfill_summary()

                        session['message'] = f"✅ Fetched and saved data for wallet: {wallet_address}"
                    else:
                        session['message'] = f"❌ Not found data for wallet: {wallet_address}"

                    return redirect(url_for("index"))

                elif action == 'filter_only':
                    return redirect(url_for("index", wallet_address=wallet_address, start_date=start_date_str, end_date=end_date_str, action='filter_only'))
                
                elif action == 'filter_by_wallet_and_chain':
                    return redirect(url_for("index", wallet_address=wallet_address, chain=chain_name, start_date=start_date_str, end_date=end_date_str, action='filter_by_wallet_and_chain'))
                
            except Exception as e:
                session['message'] = f"❌ No data found for wallet: {wallet_address} in the last 2 hours."

    elif request.method == 'GET':
        wallet_address = request.args.get('wallet_address')
        chain_name = request.args.get('chain')
        action = request.args.get('action')
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')
        npm_address = request.args.get('npm_address')
        
        # Binance API key
        # binance_api_key = session.get("binance_api_key")
        # binance_secret_key = session.get("binance_secret_key")
        
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            except ValueError:
                start_date = None

        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            except ValueError:
                end_date = None
        
        if nft_id:
            try:
                nft_id = nft_id.strip()
                offset = (page - 1) * per_page
                # nft_data = fetch_nft_history_by_id(chain, nft_id, limit=per_page, offset=offset)
                # total_items = count_nft_history_records_by_id(chain, nft_id)
                history_npm_address = npm_address if npm_address is not None else None
                nft_data = fetch_nft_history_by_id(chain, nft_id, limit=per_page, offset=offset, npm_address=history_npm_address)
                total_items = count_nft_history_records_by_id(chain, nft_id, npm_address=history_npm_address)
                total_pages = (total_items + per_page - 1) // per_page
                session['message'] = f"✅ Show history for NFT ID: {nft_id}"
            except Exception as e:
                session['message'] = f"⚠️ Error fetching history for NFT ID {nft_id}: {str(e)}"
        elif token:
            try:
                nft_data = fetch_latest_nft_id(status=None)
                if nft_data:
                    nft_data = enrich_with_pool_info(nft_data)
                    nft_data = filter_by_token(nft_data, token)
                    session['message'] = f"✅ Found {len(nft_data)} records for token: {token.lower()}"
                else:
                    session['message'] = f"❌ No data found for token: {token.lower()}"
                    
            except Exception as e:
                session['message'] = f"⚠️ Error fetching data for token {token.lower()}: {str(e)}"
        
        elif action == 'filter_only' and wallet_address:
            try:
                if is_evm_address(wallet_address):
                    checksum_wallet = Web3.to_checksum_address(wallet_address)
                elif is_solana_address(wallet_address):
                    checksum_wallet = Pubkey.from_string(wallet_address)
                else:
                    session['message'] = "⚠️ Please enter a valid wallet address."
                    return redirect(url_for("index"))
                
                # offset = (page - 1) * per_page
                print(f"Filtering by wallet {wallet_address} with dates {start_date} to {end_date}")
                # nft_data = fetch_nft_data_by_wallet_address(checksum_wallet, start_date, end_date, limit=per_page, offset=offset)
                # total_items = count_nft_by_wallet(checksum_wallet, start_date, end_date)
                # if total_items is None:
                #     total_items = 0
                # total_pages = (total_items + per_page - 1) // per_page
                
                nft_data = fetch_latest_nft_by_wallet(str(checksum_wallet))
                summary_data = fetch_latest_summary_by_token(str(checksum_wallet))
                # total_pending_cake = get_latest_total_pending_cake_by_wallet(str(checksum_wallet), start_date, end_date)
                # if total_pending_cake is None:
                #    total_pending_cake = 0
                total_pending_cake = sum(
                    nft.get("pending_cake", 0) or 0
                    for nft in nft_data
                    if nft.get("status") not in ("Closed", "Burned")
                )
    
                if checksum_wallet:
                    print("🔑 Fetching Binance Futures positions...")
                    positions = get_futures_positions_binance_data_by_wallet(str(checksum_wallet))
                    print(f"- Binance Futures positions: {positions}")

                    # Merge Binance positions vào summary
                    summary_data = merge_summary(summary_data, positions)
                else:
                    positions = []

                # print(f"- Summary data: {summary_data}")
                if nft_data:
                    session['message'] = f"✅ Show NFT data for wallet: {wallet_address}"
                else:
                    session['message'] = f"❌ No NFT data for wallet: {wallet_address}"
            except Exception as e:
                session['message'] = f"⚠️ Error: {str(e)}"
        elif action == 'filter_by_wallet_and_chain' and wallet_address and chain_name:
            try:
                if is_evm_address(wallet_address):
                    checksum_wallet = Web3.to_checksum_address(wallet_address)
                elif is_solana_address(wallet_address):
                    checksum_wallet = Pubkey.from_string(wallet_address)
                else:
                    session['message'] = "⚠️ Please enter a valid wallet address."
                    return redirect(url_for("index"))
                
                # offset = (page - 1) * per_page
                print(f"Filtering by wallet {wallet_address} and chain {chain_name} with dates {start_date} to {end_date}")
                # nft_data = fetch_nft_data_by_wallet_and_chain(chain_name, checksum_wallet, start_date, end_date, limit=per_page, offset=offset)
                # total_items = count_nft_by_wallet_and_chain(chain_name, checksum_wallet, start_date, end_date)
                # if total_items is None:
                #     total_items = 0
                # total_pages = (total_items + per_page - 1) // per_page
                
                nft_data = fetch_latest_nft_by_wallet_and_chain(str(checksum_wallet), chain_name)
                summary_data = fetch_latest_summary_by_wallet_and_chain(str(checksum_wallet), chain_name)
                # total_pending_cake = get_latest_total_pending_cake_by_wallet_and_chain(str(checksum_wallet), chain_name, start_date, end_date)
                # if total_pending_cake is None:
                #    total_pending_cake = 0
                total_pending_cake = sum(
                    nft.get("pending_cake", 0) or 0
                    for nft in nft_data
                    if nft.get("status") not in ("Closed", "Burned")
                )
                    
                if checksum_wallet:
                    print("🔑 Fetching Binance Futures positions...")
                    positions = get_futures_positions_binance_data_by_wallet(str(checksum_wallet))
                    print(f"- Binance Futures positions: {positions}")

                    # Merge Binance positions vào summary
                    summary_data = merge_summary(summary_data, positions)
                else:
                    positions = []
                    
                if nft_data:
                    session['message'] = f"✅ Show NFT data for wallet {wallet_address} on chain {chain_name}"
                else:
                    session['message'] = f"❌ No NFT data for wallet {wallet_address} on chain {chain_name}"
            except Exception as e:
                session['message'] = f"⚠️ Error: {str(e)}"
        else:
            # nft_data = fetch_nft_data(limit=per_page, offset=(page - 1) * per_page)
            # total_items = count_all_nft()
            # if total_items is None:
            #     total_items = 0
            # total_pages = (total_items + per_page - 1) // per_page
            nft_data = fetch_latest_nft_id(status='Burned')

    message = session.pop('message', None)
    has_closed = any(nft.get("status") == "Burned" for nft in nft_data)
    nft_data = enrich_nfts_with_configured_rebalancer(nft_data)

    return render_template(
        'index.html',
        nft_data=nft_data,
        summary_data=summary_data,
        total_pending_cake=total_pending_cake,
        message=message,
        wallet_address=wallet_address,
        chain_name=chain_name,
        action=action,
        start_date=start_date_str,
        end_date=end_date_str,
        page=page,
        total_pages=total_pages,
        nft_id=nft_id,
        token=token,
        has_closed=has_closed
    )
    
@app.route('/add_blacklist', methods=['POST'])
def add_blacklist_route():
    wallet_address = request.form.get('wallet_address')
    chain = request.form.get('chain')
    nft_id = request.form.get('nft_id')
    type_dex = request.form.get('type_dex') or ""
    npm_address = request.form.get('npm_address') or ""

    # result = toggle_blacklist(wallet_address, chain, nft_id)  # Đây là dict thuần
    result = toggle_blacklist(wallet_address, chain, nft_id, type_dex=type_dex, npm_address=npm_address)
    session['message'] = result['message']
    return redirect(url_for('index'))

@app.route('/remove_blacklist', methods=['POST'])
def remove_blacklist_route():
    wallet_address = request.form.get('wallet_address')
    chain = request.form.get('chain')
    nft_id = request.form.get('nft_id')
    type_dex = request.form.get('type_dex') or ""
    npm_address = request.form.get('npm_address') or ""

    # result = toggle_blacklist(wallet_address, chain, nft_id)  # Đây là dict thuần
    result = toggle_blacklist(wallet_address, chain, nft_id, type_dex=type_dex, npm_address=npm_address)
    session['message'] = result['message']
    return redirect(url_for('nft_blacklist'))

@app.route('/list-bond')
def list_bond():
    list_bond = fetch_list_bond_data()
    return render_template('bonds/list_bond.html', bonds=list_bond, title='List Bonds Apebond')

@app.route('/api/toggle_stake_track', methods=['POST'])
def toggle_stake_track():
    data = request.get_json()
    chain = data.get('chain')
    pool_address = data.get('pool_address')

    if not chain or not pool_address:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400

    return toggle_stake_track_api(chain, pool_address)

@app.route('/api/toggle_copy_bot', methods=['POST'])
def toggle_copy_bot():
    data = request.get_json()
    chain = data.get('chain')
    pool_address = data.get('pool_address')

    if not chain or not pool_address:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400

    return toggle_copy_bot_api(chain, pool_address)

@app.route('/api/toggle_stake_track_aerodrome', methods=['POST'])
def toggle_stake_track_aerodrome():
    data = request.get_json()
    chain = data.get('chain')
    pool_address = data.get('pool_address')

    if not chain or not pool_address:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400

    return toggle_stake_track_api_aerodrome(chain, pool_address)

@app.route('/add-bond', methods=['GET', 'POST'])
def add_bond():
    if request.method == 'POST':
        chain = request.form['chain']
        contract_address = request.form['contract_address']
        token_symbol = request.form['token_symbol']
        status = request.form['status']
        notify_threshold = request.form.get('notify_threshold', 10.0)

        insert_bond_data(chain, contract_address, token_symbol, status, notify_threshold)
        
        return redirect(url_for('list_bond'))
    return render_template('bonds/manage_bond.html', bond_data=None, title='Add Bond')

@app.route('/update_status/<contract_address>', methods=['POST'])
def update_status(contract_address):
    new_status = request.form['status']

    update_bond_status(contract_address, new_status)

    return redirect(url_for('list_bond'))

@app.route('/update_bond/<contract_address>', methods=['GET', 'POST'])
def update_bond(contract_address):
    if request.method == 'POST':
        chain = request.form.get('chain')
        new_contract_address = request.form.get('contract_address')
        token_symbol = request.form.get('token_symbol')
        status = request.form.get('status')
        notify_threshold = request.form.get('notify_threshold', 10.0)

        update_bond_data(chain, new_contract_address, token_symbol, status, notify_threshold, contract_address)
        return redirect(url_for('list_bond'))

    # GET: lấy bond hiện tại từ DB
    bond_data = fetch_bond_data_by_contract_address(contract_address)
    return render_template('bonds/manage_bond.html', bond_data=bond_data, title='Edit Bond')

@app.route('/delete_bond/<contract_address>', methods=['POST'])
def delete_bond(contract_address):
    delete_bond_contract(contract_address)
    
    return redirect(url_for('list_bond'))

@app.route('/api/update_bond_threshold', methods=['POST'])
def api_update_bond_threshold():
    data = request.get_json()
    contract_address = data.get('contract_address')
    notify_threshold = data.get('notify_threshold')
    if not contract_address or notify_threshold is None:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400
    success = update_bond_threshold(contract_address, notify_threshold)
    if success:
        return jsonify({'success': True, 'message': 'Threshold updated successfully'})
    else:
        return jsonify({'success': False, 'message': 'Failed to update threshold'}), 500

@app.route('/update_bonds_from_api')
def update_bonds_from_api():
    fetch_and_update_bonds()
    return redirect(url_for('list_bond'))
    
@app.route('/nft_blacklist')
def nft_blacklist():
    nft_blacklist = fetch_blacklist_nft_ids()
    return render_template('nft_blacklist.html', nft_blacklist=nft_blacklist, title='NFT ID Blacklist')

@app.route('/configured-rebalancer')
def configured_rebalancer_view():
    context = get_configured_rebalancer_context()
    return render_template(
        'pools/configured_rebalancer.html',
        **context,
        title='Configured Rebalancer'
    )

def convert_timestamps(pool_list):
    for pool in pool_list:
        if "open_time" in pool and "end_time" in pool:
            pool["open_time"] = datetime.fromtimestamp(
                pool["open_time"], tz=UTC_PLUS_7
            ).strftime("%Y-%m-%d %H:%M:%S")

            pool["end_time"] = datetime.fromtimestamp(
                pool["end_time"], tz=UTC_PLUS_7
            ).strftime("%Y-%m-%d %H:%M:%S")

        if "epoch_start" in pool and "epoch_finish" in pool:
            pool["epoch_start"] = datetime.fromtimestamp(
                pool["epoch_start"], tz=UTC_PLUS_7
            ).strftime("%Y-%m-%d %H:%M:%S")

            pool["epoch_finish"] = datetime.fromtimestamp(
                pool["epoch_finish"], tz=UTC_PLUS_7
            ).strftime("%Y-%m-%d %H:%M:%S")

    return pool_list

@app.route('/list_pool')
def list_pool():
    pools = fetch_all_pool_info()
    pools = convert_timestamps(pools)
    
    # Enrich EVM pools with validation
    for p in pools:
        p['validation'] = validate_pool_backend(p)
    pools, rebalancer_pool_summary = enrich_pools_with_configured_rebalancer(pools)

    total_cake_per_day_chain = get_total_cake_per_day_each_chain()
    total_weekly_rewards_sol = get_total_weekly_rewards_sol()
    total_cake_per_day_sol = total_weekly_rewards_sol / 7
    
    # Fetch and filter Sol pools (Only show pools with farm)
    raw_pools_sol = fetch_all_pool_sol_info()
    filtered_pools_sol = [p for p in raw_pools_sol if p.get('weekly_rewards', 0) > 0]
    pools_sol = convert_timestamps(filtered_pools_sol)
    
    # Enrich Sol pools with validation
    for p in pools_sol:
        p['validation'] = validate_pool_backend(p)

    now_str = datetime.now(UTC_PLUS_7).strftime("%Y-%m-%d %H:%M:%S")

    explorers = {
        "ARB":"https://pancakeswap.finance/liquidity/pool/arb/",
        "BAS":"https://pancakeswap.finance/liquidity/pool/base/",
        "BNB":"https://pancakeswap.finance/liquidity/pool/bsc/",
        "ETH":"https://pancakeswap.finance/liquidity/pool/eth/",
        "LIN":"https://pancakeswap.finance/liquidity/pool/linea/",
        "POL":"https://pancakeswap.finance/liquidity/pool/polygon-zkevm/",
        "SOL":"https://solana.pancakeswap.finance/clmm/create-position/?pool_id="
    }
    
    return render_template(
        'pools/list_pool.html', 
        pools=pools, 
        pools_sol=pools_sol, 
        total_cake_per_day_chain=total_cake_per_day_chain, 
        total_cake_per_day_sol=total_cake_per_day_sol, 	
        rebalancer_pool_summary=rebalancer_pool_summary,
        explorers=explorers, 
        now=now_str, 
        title='List Pool Farm'
    )

@app.route("/api/transactions", methods=['POST'])
def get_transactions():
  filters = request.get_json()
  wallet_address = filters.get("walletAddress")
  chains = filters.get("chains")
  date_from = filters.get("dateFrom")
  date_to = filters.get("dateTo")
  symbol = filters.get("symbol")
  contract_address = filters.get("contract")
  print(f"{wallet_address}, {chains}, {date_from}, {date_to}, {symbol}, {contract_address}")

  existing_wallet = get_existing_wallet(wallet_address)
  if existing_wallet:
    transaction_history = get_transaction(wallet_address, chains, date_from, date_to, symbol, contract_address)

  else:
    if chains[0] == "SOL":
      transaction_history = get_transaction_sol_with_filter(wallet_address, date_from, date_to, symbol, contract_address)
    else:
      transaction_history = get_transactions_with_filter(wallet_address, chains, date_from, date_to, symbol,
                                                     contract_address)
  print(transaction_history)
  return jsonify(transaction_history)

@app.route("/transactions-v2", methods=['GET'])
def view_transactions_v2():
  return render_template("transactions/transactions_v2.html", title='Transactions History V2')

@app.route("/api/transactions-v2", methods=['POST'])
def get_transactions_v2():
  filters = request.get_json()
  wallet_address = filters.get("walletAddress")
  chains = filters.get("chains")
  date_from = filters.get("dateFrom")
  date_to = filters.get("dateTo")
  symbol = filters.get("symbol")
  contract_address = filters.get("contract")
  print(f"[V2] {wallet_address}, {chains}, {date_from}, {date_to}, {symbol}, {contract_address}")

  existing_wallet = get_existing_wallet_v2(wallet_address)
  if existing_wallet:
    transaction_history = get_transaction_v2(wallet_address, chains, date_from, date_to, symbol, contract_address)
  else:
    if chains[0] == "SOL":
      transaction_history = get_transaction_sol_with_filter_v2(wallet_address, date_from, date_to, symbol, contract_address)
    else:
      transaction_history = get_transactions_with_filter_v2(wallet_address, chains, date_from, date_to, symbol, contract_address)
      
  return jsonify(transaction_history)

# ─── Valid chain identifiers for NFT search ───────────────────────────────────
_VALID_NFT_SEARCH_CHAINS = {"ETH", "BAS", "BSC", "POL", "ARB", "LIN", "SOL"}

@app.route("/api/nft-tx-search", methods=["GET", "POST"])
def nft_tx_search():
  """
  Search transaction history by NFT ID (partial match), optionally filtered by chain.

  GET  : /api/nft-tx-search?nft_id=123&chain=BAS
  POST : { "nft_id": "123", "chain": "BAS" }   (chain is optional)

  Rules:
    - nft_id  : required, 1–100 chars, partial match supported
    - chain   : optional; omit / "" → all chains;
                "SOL" → Solana; "BAS"/"ETH"/etc. → specific EVM chain

  Response mirrors get_transaction_v2 + extra fields:
    { nft_id_query, chain, count, max_length, total, transactions: [...] }
  """
  # ── Parse input (GET or POST) ────────────────────────────────────────────
  if request.method == "POST":
    data = request.get_json(force=True) or {}
    nft_id = str(data.get("nft_id", "")).strip()
    chain  = str(data.get("chain",  "")).strip() or None
  else:
    nft_id = request.args.get("nft_id", "").strip()
    chain  = request.args.get("chain",  "").strip() or None

  # ── Validation ───────────────────────────────────────────────────────────
  if not nft_id:
    return jsonify({"error": "nft_id is required"}), 400
  if len(nft_id) > 100:
    return jsonify({"error": "nft_id must be 100 characters or fewer"}), 400
  if chain and chain.upper() not in _VALID_NFT_SEARCH_CHAINS:
    return jsonify({"error": f"Invalid chain '{chain}'. Valid values: {sorted(_VALID_NFT_SEARCH_CHAINS)}"}), 400

  chain_upper = chain.upper() if chain else None
  print(f"[NFT-TX-SEARCH] nft_id='{nft_id}' chain={chain_upper}")

  result = search_tx_by_nft_id(nft_id, chain_upper)
  result["nft_id_query"] = nft_id
  result["chain"]        = chain_upper
  return jsonify(result)

CACHE = {}
CACHE_TTL = 600  # 10 phút

def get_cache(amm, pool_id):
    key = f"{amm}:{pool_id}"
    if key in CACHE:
        data, ts = CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return data
        else:
            # hết hạn, xóa
            del CACHE[key]
    return None

def set_cache(amm, pool_id, data):
    key = f"{amm}:{pool_id}"
    CACHE[key] = (data, time.time())


@app.route("/price_range_pool_sol/<pool_id>", methods=['GET', 'POST'])
def price_range_pool_sol(pool_id):
    # pool_id = None
    amm = "pancake"   # mặc định pancake
    pool_ranges = None

    if request.method == 'POST':
        pool_id = request.form.get('pool_id') or pool_id
        amm = request.form.get('amm', 'pancake')
        
        if pool_id and amm:
            pool_ranges = get_cache(amm, pool_id)
            if not pool_ranges:
                if amm == "pancake":
                    session['message'] = "✅ Analyzing PancakeSwap pool..."
                    pool_ranges = analyze_pool_ticks(
                        HELIUS_CLIENT,
                        PANCAKE_PROGRAM_ID,
                        Pubkey.from_string(pool_id)
                    )
                elif amm == "raydium":
                    session['message'] = "✅ Analyzing Raydium pool..."
                    pool_ranges = analyze_pool_ticks(
                        HELIUS_CLIENT,
                        RAYDIUM_PROGRAM_ID,
                        Pubkey.from_string(pool_id)
                    )
                else:
                    session['message'] = "❌ Unsupported AMM selected."
                    pool_ranges = None

                set_cache(amm, pool_id, pool_ranges)

            # session["last_pool_id"] = pool_id
            session["last_amm"] = amm
    else:
        # pool_id = session.get("last_pool_id")
        # amm = session.get("last_amm", "pancake")
        # if pool_id and amm:
            
        pool_ranges = get_cache(amm, pool_id)

    message = session.pop('message', None)
    
    return render_template(
        "pool_ranges/price_range_pool_sol.html",
        pool_id=pool_id,
        amm=amm,
        pool_ranges=pool_ranges,
        message=message,
        title='Price Range Pool Sol'
    )

@app.route("/api/current-tick-update")
def api_current_tick_update():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT t1.chain, t1.pool_address, t1.current_price
        FROM wallet_nft_position t1
        INNER JOIN (
            SELECT latest_pool.chain, latest_pool.pool_address, MAX(w.id) AS max_id
            FROM wallet_nft_position w
            INNER JOIN (
                SELECT chain, pool_address, MAX(created_at) AS max_time
                FROM wallet_nft_position
                WHERE status != 'Burned'
                    AND pool_address IS NOT NULL
                    AND pool_address != ''
                GROUP BY chain, pool_address
            ) latest_pool ON w.chain = latest_pool.chain
                AND w.pool_address = latest_pool.pool_address
                AND w.created_at = latest_pool.max_time
            WHERE w.status != 'Burned'
            GROUP BY latest_pool.chain, latest_pool.pool_address
        ) t2 ON t1.id = t2.max_id
            AND t1.chain = t2.chain
            AND t1.pool_address = t2.pool_address
        LEFT JOIN nft_blacklist b
            ON t1.chain = b.chain
            AND t1.nft_id = b.nft_id
            AND (b.type_dex = t1.type_dex OR COALESCE(b.type_dex, '') = '')
            AND (
                COALESCE(b.npm_address, '') = COALESCE(t1.npm_address, '')
                OR (t1.type_dex = 'aerodrome' AND COALESCE(b.npm_address, '') = '')
            )
        WHERE b.id IS NULL
        ORDER BY t1.created_at DESC
    """
    cursor.execute(query)
    results = cursor.fetchall()
    return jsonify(results)

@app.route("/transactions", methods=['GET'])
def view_transactions():
  return render_template("transactions/transactions.html", title='Transactions History')

@app.route('/mint_position/<chain>/<pool_address>/<min_price>/<max_price>')
def mint_position_data(chain, pool_address, min_price, max_price):
    if chain == "SOL":
        mint_data = get_data_mint_sol(chain, CLIENT, pool_address)
        return render_template(
                                'pools_liquidity/mint_position_sol.html', 
                                mint_data=mint_data, 
                                chain=chain, 
                                pool_address=pool_address,
                                min_price=min_price,
                                max_price=max_price,
                               )
    
    mint_data = get_data_mint(chain, pool_address)
    return render_template(
            'pools_liquidity/mint_position.html', 
            mint_data=mint_data, 
            chain=chain, 
            pool_address=pool_address, 
            min_price=min_price,
            max_price=max_price
        )

def safe_float(value: str):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip().replace("e ", "e").replace("E ", "E")
        try:
            return float(value)
        except ValueError:
            return None  # hoặc raise Exception tùy logic
    return None

def convert_token_amount(amount: float, decimals: int) -> int:
    """Convert readable token amount -> raw integer based on decimals"""
    if amount is None:
        return 0
    return int(amount * (10 ** decimals))

@app.route("/get_mint_params")
def get_mint_params_api_sol():
    chain = request.args.get("chain")
    pool_address = request.args.get("pool_address")
    min_price = safe_float(request.args.get("min_price", 100))
    max_price = safe_float(request.args.get("max_price", 150))
    amount0 = safe_float(request.args.get("amount0"))
    amount1 = safe_float(request.args.get("amount1"))
    payer_pubkey = request.args.get("payer")
    token0_decimals = int(request.args.get("token0_decimals"))
    token1_decimals = int(request.args.get("token1_decimals"))
    print(f"chain: {chain}, pool_address: {pool_address}, min_price: {min_price}, max_price: {max_price}, payer_pubkey: {payer_pubkey}, token0_decimals: {token0_decimals}, token1_decimals: {token1_decimals}")
    
    amount_0_max = convert_token_amount(amount0, token0_decimals)
    amount_1_max = convert_token_amount(amount1, token1_decimals)
    print(f"amount0: {amount0}, amount1: {amount1}")
    print(f"amount_0_max: {amount_0_max}, amount_1_max: {amount_1_max}")
    amount_1_max = math.ceil(amount_1_max)
    amount_1_max = int(amount_1_max * 1.0006)  # thêm buffer 0.03%
    print(f"Final amount_1_max with buffer: {amount_1_max}")
    
    if chain == "SOL":
        params = get_mint_params_sol(chain, CLIENT, PANCAKE_PROGRAM_ID, pool_address, min_price, max_price, payer_pubkey)
        results = build_mint_position_tx_sol_v9(
            CLIENT, 
            payer_pubkey,
            payer_pubkey,
            pool_address,    
            params,
            amount_0_max,
            amount_1_max,
            liquidity=0,
            with_metadata=True,
            base_flag=True,
        )
        rent_lamports = CLIENT.get_minimum_balance_for_rent_exemption(82).value
        balance = CLIENT.get_balance(Pubkey.from_string(payer_pubkey)).value
        rent_ata_token0 = CLIENT.get_minimum_balance_for_rent_exemption(170).value
        rent_ata_token1 = CLIENT.get_minimum_balance_for_rent_exemption(170).value
        
        print("Rent mint:", rent_lamports / 1e9, "SOL")
        print("Rent ATA token0:", rent_ata_token0 / 1e9, "SOL")
        print("Rent ATA token1:", rent_ata_token1 / 1e9, "SOL")
        print("User balance:", balance / 1e9, "SOL")
        
        print((results))
        print(f"params: {params}")
        
    return jsonify(results)

# ========= REFRESH NFT DATA =========
@app.route("/refresh", methods=["POST"])
def refresh_nft():
    data = request.json
    chain = data.get("chain")
    wallet_address = data.get("wallet_address")
    nft_id = data.get("nft_id")
    dex = data.get("dex")
    npm_address = data.get("npm_address")
    print(f"Refresh request - chain: {chain}, wallet_address: {wallet_address}, nft_id: {nft_id}, dex: {dex}, npm_address: {npm_address}")
    # print(f"Refresh request - chain: {chain}, wallet_address: {wallet_address}, nft_id: {nft_id}, dex: {dex}")

    if not chain or not wallet_address or not nft_id:
        return jsonify({"error": "Missing parameter"}), 400
    if chain == "SOL":
        success, nft_data = process_nft(nft_id, chain, wallet_address)
    else:
        if dex != "aerodrome":
            success, nft_data = process_nft_evm(nft_id, chain, wallet_address)
        else:
            # success, nft_data = process_nft_evm_aerodrome(nft_id, chain, wallet_address)
            success, nft_data = process_nft_evm_aerodrome(nft_id, chain, wallet_address, npm_address=npm_address)
    
    if not success:
        return jsonify({"status": "error",
                        "chain": chain, 
                        "wallet": wallet_address, 
                        "nft_id": nft_id,
                        "data": nft_data
                       }), 500

    return jsonify({
        "status": "done",
        "chain": chain,
        "wallet": wallet_address,
        "nft_id": nft_id,
        "data": nft_data
    })

mint_service = MintingService(rpc_url=QUICKNODE_RPC_URL, jupiter_api_key=JUPITER_API_KEY)

@app.route('/semi-auto-mint', methods=['GET', 'POST'])
def semi_auto_mint_view():
    pool_address = request.args.get('pool_address', '')
    return render_template(
        'pools_liquidity/semi_auto_mint.html',
        pool_address=pool_address,
        title='Semi-Automatic Minting'
    )

@app.route('/api/mint/init', methods=['POST'])
def init_pool_data():
    try:
        data = request.get_json()
        pool_address = data.get('pool_address')
        
        if not pool_address:
            return jsonify({'error': 'Missing pool_address'}), 400

        # Gọi Service Module 1
        print(f"🔄 Scanning pool: {pool_address}")
        context_data = mint_service.get_pool_and_best_position(pool_address)
        
        return jsonify({
            'status': 'success',
            'data': context_data
        })
    except Exception as e:
        print(f"❌ Error in init_pool_data: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/mint/calculate', methods=['POST'])
def calculate_plan():
    try:
        data = request.get_json()
        user_wallet = data.get('user_wallet')
        multiplier = float(data.get('multiplier', 1.0))
        pool_context_data = data.get('context_data')
        slippage_bps = int(data.get('slippage_bps', 50))

        if not user_wallet or not pool_context_data:
            return jsonify({'error': 'Missing required data'}), 400

        # Gọi Service Module 2, 3, 4
        print(f"🧮 Calculating plan for {user_wallet} with x{multiplier}")
        plan = mint_service.calculate_mint_plan(user_wallet, multiplier, pool_context_data, slippage_bps)
        
        return jsonify({
            'status': 'success',
            'data': plan
        })
    except Exception as e:
        print(f"❌ Error in calculate_plan: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
### Semi-Automatic Minting EVM Chain ###
@app.route('/semi-auto-mint-evm', methods=['GET', 'POST'])
def semi_auto_mint_evm_view():
    chain = request.args.get('chain', '')
    pool_address = request.args.get('pool_address', '')
    dex_type = request.args.get('dex_type', '')

    return render_template(
        'pools_liquidity/semi_auto_mint_evm.html',
        pool_address=pool_address, chain=chain, dex_type=dex_type,
        title='Semi-Automatic Minting EVM Chain'
    )

@app.route('/api/v3/pool-metadata', methods=['GET'])
def get_ui_metadata():
    pool = request.args.get('pool_address', '')
    chain = request.args.get('chain', 'BNB').upper()
    dex_type = request.args.get('dex_type', '')
    # Update mặc định luôn là True để phá cache cũ khi load giao diện, đảm bảo On-chain real-time ngay tắp lự
    force_refresh_str = request.args.get('refresh', 'true')
    force_refresh = force_refresh_str.lower() == 'true'
    print(f"UI Metadata request for {pool} on {chain} (dex_type={dex_type or 'auto'}, force_refresh={force_refresh})")
    
    if not pool:
        return jsonify({"error": "MISSING_POOL_ADDRESS"}), 400
        
    rpc_url = RPC_URLS.get(chain)
    if not rpc_url:
        return jsonify({"error": "UNSUPPORTED_CHAIN"}), 400

    try:
        aggregator = V3ApiAggregator(chain, rpc_url, dex_type=dex_type)
        metadata = aggregator.get_ui_metadata(pool, force_refresh, dex_type=dex_type)
        
        print(f"Metadata for {pool}: {metadata}")
        return jsonify(metadata)
    except Exception as e:
        return jsonify({"error": "INTERNAL_SERVER_ERROR", "message": str(e)}), 500

@app.route('/api/v3/generate-plan', methods=['POST'])
def generate_plan():
    data = request.json
    print(f"Generate plan request - data: {data}")
    chain = data.get('chain', 'BNB').upper()
    dex_type = data.get('dex_type', '')
    agg = V3ApiAggregator(chain, RPC_URLS.get(chain), dex_type=dex_type)
    return jsonify(agg.get_execution_plan(
        pool_address=data['pool'],
        user_address=data['user'],
        capital_usd=data['capital'],
        mode=data['mode'],
        custom_range=data.get('custom_range'),
        slippage_bps=data.get('slippage', 50),
        force_refresh=data.get('refresh', False),
        quote_mode=data.get('quote_mode', 'full'),
        dex_type=dex_type
    ))

### COLLECT FEE & HARVEST REWARDS (PancakeSwap V3 EVM) ###
@app.route('/api/v3/claimable-info', methods=['POST'])
def api_claimable_info():
    """Lấy thông tin fee + reward pending cho 1 position"""
    try:
        data = request.json
        chain = data.get('chain', '').upper()
        nft_id = int(data.get('nft_id'))
        dex = data.get('dex', 'pancakeswap')
        npm_address = data.get('npm_address') or None
        user_address = data.get('user_address') or data.get('wallet_address') or None
        if not chain or not nft_id:
            return jsonify({'error': 'Missing chain or nft_id'}), 400
        # Skip unsupported chains
        if chain in ('MON', 'SOL'):
            return jsonify({'error': 'UNSUPPORTED_CHAIN', 'message': f'{chain} does not support collect/harvest yet'}), 400
        service = CollectHarvestService(chain, dex=dex, npm_address=npm_address)
        info = service.get_claimable_info(nft_id, user_address=user_address)
        return jsonify(info)
    except Exception as e:
        log.error(f"❌ Error in api_claimable_info: {e}")
        return jsonify({'error': 'INTERNAL_ERROR', 'message': str(e)}), 500

@app.route('/api/v3/build-collect-tx', methods=['POST'])
def api_build_collect_tx():
    """Build unsigned tx cho Collect Fee"""
    try:
        data = request.json
        chain = data.get('chain', '').upper()
        nft_id = int(data.get('nft_id'))
        user_address = data.get('user_address', '')
        dex = data.get('dex', 'pancakeswap')
        npm_address = data.get('npm_address') or None
        if not chain or not nft_id or not user_address:
            return jsonify({'error': 'Missing required parameters'}), 400
        if chain in ('MON', 'SOL'):
            return jsonify({'error': 'UNSUPPORTED_CHAIN', 'message': f'{chain} does not support collect yet'}), 400
        service = CollectHarvestService(chain, dex=dex, npm_address=npm_address)
        tx_data = service.build_collect_fee_tx(nft_id, user_address)
        return jsonify(tx_data)
    except Exception as e:
        log.error(f"❌ Error in api_build_collect_tx: {e}")
        return jsonify({'error': 'INTERNAL_ERROR', 'message': str(e)}), 500

@app.route('/api/v3/build-harvest-tx', methods=['POST'])
def api_build_harvest_tx():
    """Build unsigned tx cho Harvest CAKE Reward"""
    try:
        data = request.json
        chain = data.get('chain', '').upper()
        nft_id = int(data.get('nft_id'))
        user_address = data.get('user_address', '')
        dex = data.get('dex', 'pancakeswap')
        npm_address = data.get('npm_address') or None
        if not chain or not nft_id or not user_address:
            return jsonify({'error': 'Missing required parameters'}), 400
        if chain in ('MON', 'SOL'):
            return jsonify({'error': 'UNSUPPORTED_CHAIN', 'message': f'{chain} does not support harvest yet'}), 400
        service = CollectHarvestService(chain, dex=dex, npm_address=npm_address)
        tx_data = service.build_harvest_reward_tx(nft_id, user_address)
        return jsonify(tx_data)
    except Exception as e:
        log.error(f"❌ Error in api_build_harvest_tx: {e}")
        return jsonify({'error': 'INTERNAL_ERROR', 'message': str(e)}), 500

@app.route('/api/v3/build-withdraw-tx', methods=['POST'])
def api_build_withdraw_tx():
    """Build unsigned multicall tx cho Withdraw Position hoàn toàn"""
    try:
        data = request.json
        chain = data.get('chain', '').upper()
        nft_id = int(data.get('nft_id'))
        user_address = data.get('user_address', '')
        dex = data.get('dex', 'pancakeswap')
        npm_address = data.get('npm_address') or None
        if not chain or not nft_id or not user_address:
            return jsonify({'error': 'Missing required parameters'}), 400
        if chain in ('MON', 'SOL'):
            return jsonify({'error': 'UNSUPPORTED_CHAIN', 'message': f'{chain} does not support withdraw yet'}), 400
        service = CollectHarvestService(chain, dex=dex, npm_address=npm_address)
        tx_data = service.build_withdraw_tx(nft_id, user_address)
        return jsonify(tx_data)
    except Exception as e:
        log.error(f"❌ Error in api_build_withdraw_tx: {e}")
        return jsonify({'error': 'INTERNAL_ERROR', 'message': str(e)}), 500

### AERODROME DEX ### 
@app.route('/list_pool/aerodrome')
def list_pool_aerodrome():
    pools = fetch_all_pool_info_aerodrome_db()
    total_aero_per_day_each_chain = get_total_aero_per_day_each_chain()
    aero_pools = convert_timestamps(pools)
    for p in aero_pools:
        p['validation'] = validate_pool_backend(p)
 
    explorers = {
        "BAS": "https://aerodrome.finance/deposit?"
    }
    
    return render_template(
        'pools/list_pool_aerodrome.html', pools=aero_pools, total_aero_per_day_each_chain=total_aero_per_day_each_chain, explorers=explorers, title='List Pool Farm on Aerodrome'
    )

def job_fetch_token():
    try:
        log.info(f"[Job 1] Bắt đầu fetch token lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/fetch_token_cmc_id.sh"], check=True, cwd="/app", timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 1] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 1] Lỗi: {e}")

def job_nft_pancake_fetcher():
    try:
        log.info(f"[Job 2] Bắt đầu chạy nft_pancake_fetcher lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/nft_pancake.sh"], check=True, cwd="/app", timeout=5400)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 2] Timeout sau 90 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 2] Lỗi: {e}")

def job_monitor_evm_pool():
    try:
        log.info(f"[Job 3] Bắt đầu chạy monitor_evm_pool lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/monitor_evm_pool.sh"], check=True, cwd="/app", timeout=3600)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 3] Timeout sau 60 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 3] Lỗi: {e}")

def job_cron_pool_epoch_state():
    try:
        log.info(f"[Job 4] Bắt đầu chạy cron_pool_epoch_state lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/cron_pool_epoch_state.sh"], check=True, cwd="/app", timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 4] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 4] Lỗi: {e}")

def job_update_evm_staked_lp():
    try:
        log.info(f"[Job 4] Bắt đầu chạy update_evm_staked_lp lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/update_evm_staked_lp.sh"], check=True, timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 5] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 5] Lỗi: {e}")

def job_auto_mint_position():
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg = f"[Job 6] Bắt đầu chạy auto_mint_position lúc {now_str}"
        # Ghi trực tiếp vào file log cơ bản để bypass log system nếu nó đang bị treo
        with open("/app/logs/job_debug.log", "a", encoding="utf-8") as f:
            f.write(msg)
            
        log.info(msg.strip())
        subprocess.run(["sh", "/app/cronjobs/auto_mint_position.sh"], check=True, cwd="/app", timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 6] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 6] Lỗi thực thi: {e}")
        with open("/app/logs/job_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[Job 6] ERROR: {e}")

def job_auto_rebalance_position():
    try:
        log.info(f"[Job 7] Bắt đầu chạy auto_rebalance_position lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/auto_rebalance_position.sh"], check=True, timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 7] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 7] Lỗi: {e}")

def job_apebond_notify():
    try:
        log.info(f"[Job 8] Bắt đầu chạy apebond_notify lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/bond_update.sh"], check=True, cwd="/app", timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 8] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 8] Lỗi thực thi: {e}")

def job_fetch_tx_history():
    try:
        log.info(f"[Job 9] Bắt đầu chạy fetch_tx_history lúc {datetime.now()}")
        subprocess.run(["sh", "/app/cronjobs/fetch_tx_history.sh"], check=True, cwd="/app", timeout=1800)
    except subprocess.TimeoutExpired:
        log.error(f"[Job 9] Timeout sau 30 phút, bỏ qua.")
    except Exception as e:
        log.error(f"[Job 9] Lỗi thực thi: {e}")

scheduler_socket = None

# def start_scheduler_socket_lock():
#     global scheduler_socket
#     try:
#         # Tạo một socket TCP
#         scheduler_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
#         scheduler_socket.bind(("127.0.0.1", 49999))
        
#     except socket.error:
#         log.info("Scheduler lock is busy. Scheduler is already running in another worker.")
#         return
#     else:
#         log.info("Scheduler lock acquired. Starting Scheduler...")
        
#         # --- KHỞI TẠO SCHEDULER Ở ĐÂY ---
#         # scheduler = BackgroundScheduler(timezone='Asia/Ho_Chi_Minh')

#         # scheduler.add_job(func=job_fetch_token, trigger="cron", hour=7, minute=0)
#         # scheduler.add_job(func=job_nft_pancake_fetcher, trigger="cron", hour='*/2', minute=0)
#         # scheduler.add_job(func=job_monitor_evm_pool, trigger="cron", hour='1-23/2', minute=0)
#         # scheduler.add_job(func=job_cron_pool_epoch_state, trigger="cron", hour=7, minute=10)
#         # scheduler.add_job(func=job_update_evm_staked_lp, trigger="cron", minute='*/20')
#         # scheduler.add_job(func=job_auto_mint_position, trigger="cron", minute=25)
#         # scheduler.add_job(func=job_auto_rebalance_position, trigger="cron", minute=5)
#         # scheduler.add_job(func=job_apebond_notify, trigger="cron", minute=30)
#         # scheduler.add_job(func=job_fetch_tx_history, trigger="cron", minute='*/10')

#         # scheduler.start()
# ============================================
# USER NOTE ROUTES
# ============================================
# ============================================
# USER NOTE ROUTES - Create & Update
# ============================================

# API cập nhật user_note (PUT)
@app.route('/api/user_note/<int:note_id>', methods=['PUT'])
def update_user_note(note_id):
    data = request.get_json()
    note_text = data.get('user_note', '').strip()
    symbol = data.get('symbol', '') or ''
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Dùng Python tính giờ Việt Nam
    vietnam_now = datetime.now(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute("""
        UPDATE user_note 
        SET user_note = %s, symbol = %s, updated_at = %s
        WHERE id = %s
    """, (note_text, symbol, vietnam_now, note_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Note updated successfully'})


# API tạo mới user_note (POST)
@app.route('/api/user_note', methods=['POST'])
def create_user_note():
    data = request.get_json()
    chain = data.get('chain')
    contract_address = data.get('contract_address')
    symbol = data.get('symbol', '') or ''          
    note_text = data.get('user_note', '').strip()
    
    conn = get_connection()
    cursor = conn.cursor()
    
    vietnam_now = datetime.now(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute("""
        INSERT INTO user_note (chain, contract_address, symbol, user_note, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE user_note = VALUES(user_note), symbol = VALUES(symbol), updated_at = VALUES(updated_at)
    """, (chain, contract_address, symbol, note_text, vietnam_now, vietnam_now))
    
    cursor.execute("SELECT id FROM user_note WHERE chain = %s AND contract_address = %s", (chain, contract_address))
    result = cursor.fetchone()
    note_id = result[0] if result else None
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'id': note_id, 'message': 'Note created successfully'})
# start_scheduler_socket_lock()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True)
