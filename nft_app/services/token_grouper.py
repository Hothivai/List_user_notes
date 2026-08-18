import re

def extract_base_symbol(symbol: str) -> str:
    """
    Trích xuất token gốc từ symbol một cách triệt để:
    - Loại bỏ ký tự rác \x00, chuẩn hóa ₮ -> T
    - Nhóm các biến thể ETH (WETH, cbETH, wstETH, ezETH, bsdETH, rETH, msETH, LsETH, superOETHb...) -> ETH
    - Nhóm các biến thể USDT (USDT, USD₮0, USDT0, oUSDT...) -> USDT
    - Nhóm các biến thể USDC (USDC, USDbC, axlUSDC...) -> USDC
    - Nhóm các đồng USD Stablecoin khác -> USD
    - Loại bỏ Suffix chuỗi xích & hậu tố (.E, .B, (OLD), V2, V3, 0, 1, 2, B, E, S) như SNDKB -> SNDK, SPCXB -> SPCX
    - Loại bỏ Prefix chuỗi xích & tiền tố (WORMHOLE, AXL, ST, CB, WE, FX, W, M, V, B, O, C)
    """
    if not symbol:
        return symbol

    # 1. Làm sạch & chuẩn hóa ký tự đặc biệt
    s = str(symbol).replace('₮', 'T').replace('\x00', '').strip().upper()

    # 2. Trường hợp đặc biệt token quản trị
    if s in ['ETHFI', 'ETHFI.E']:
        return 'ETHFI'

    # 3. Gom nhóm BTC, ETH, SOL & Stablecoins
    if 'BTC' in s or 'SOLV' in s:
        if 'SOLVBTC' in s or 'WBTC' in s or 'BTCB' in s or 'BTC' in s:
            return 'BTC'
    if 'SOL' in s and not ('SOLV' in s or 'SOLAR' in s):
        return 'SOL'
    if 'ETH' in s:
        return 'ETH'
    if 'USDT' in s or s in ['USDT0', 'OUSDT', 'WUSDT']:
        return 'USDT'
    if 'USDC' in s or s in ['USDBC', 'AXLUSDC', 'WUSDC']:
        return 'USDC'
    if 'BUSD' in s:
        return 'BUSD'
    if 'USD' in s:
        return 'USD'

    # 4. Loại bỏ Suffix hậu tố (VD: SNDKB -> SNDK, SPCXB -> SPCX, TOKEN.E -> TOKEN)
    suffixes = ['.E', '.B', ' (OLD)', '.OLD', 'V2', 'V3', 'OLD', '0', '1', '2', 'B', 'E', 'S']
    for suf in suffixes:
        if s.endswith(suf) and len(s) > len(suf):
            candidate = s[:-len(suf)]
            if len(candidate) >= 3:
                s = candidate
                break

    # 5. Loại bỏ Prefix tiền tố (VD: cbXRP -> XRP, wSOL -> SOL)
    prefixes = ['WORMHOLE', 'AXL', 'ST', 'CB', 'WE', 'FX', 'W', 'M', 'V', 'B', 'O', 'C']
    for p in prefixes:
        if s.startswith(p) and len(s) > len(p):
            candidate = s[len(p):]
            if len(candidate) >= 3:
                s = candidate
                break

    return s


def get_grouped_tokens_from_list(token_list=None):
    """
    Nhận danh sách token (dict có chain, contract_address, symbol, ...)
    Trả về danh sách nhóm, mỗi nhóm có primary và variants.
    """
    if not token_list:
        return []

    groups_dict = {}
    for token in token_list:
        base = extract_base_symbol(token.get('symbol', ''))
        chain = token.get('chain', '')
        if not base or not chain:
            continue
        key = f"{base}_{chain}"
        if key not in groups_dict:
            groups_dict[key] = {
                'base_symbol': base,
                'chain': chain,
                'tokens': []
            }
        groups_dict[key]['tokens'].append(token)

    result = []
    for key, group_data in groups_dict.items():
        tokens = group_data['tokens']
        # Chọn primary: ưu tiên token có symbol == base_symbol, ngược lại chọn token đầu tiên
        primary = None
        variants = []
        for t in tokens:
            if t.get('symbol', '').upper() == group_data['base_symbol']:
                primary = t
                break
        if primary is None and tokens:
            primary = tokens[0]
            variants = tokens[1:]
        else:
            variants = [t for t in tokens if t != primary]

        result.append({
            'group_key': key,
            'base_symbol': group_data['base_symbol'],
            'chain': group_data['chain'],
            'primary': primary,
            'variants': variants
        })
    return result

# Alias để tương thích ngược
group_tokens_from_list = get_grouped_tokens_from_list