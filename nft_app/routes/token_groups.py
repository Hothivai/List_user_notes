from flask import Blueprint, jsonify, request, render_template
from datetime import datetime, timezone, timedelta
from services.db_connect import get_connection
from utils.token_aggregator import get_grouped_token_list, ensure_token_group_mappings_table

token_groups_bp = Blueprint('token_groups', __name__)
UTC_PLUS_7 = timezone(timedelta(hours=7))

@token_groups_bp.route('/list_token_notes', methods=['GET'])
def list_token_notes():
    """Route hiển thị danh sách token với dropdown biến thể nhóm"""
    grouped_tokens = get_grouped_token_list()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        'pools/list_token_notes.html',
        tokens=grouped_tokens,
        now=now_str,
        title='List Token with Perpetual & User Notes'
    )

@token_groups_bp.route('/api/token-groups/merge', methods=['POST'])
def api_merge_tokens():
    """API gộp các token con (token_identifiers) vào token chính (primary_identifier)"""
    data = request.get_json() or {}
    primary_id = data.get('primary_identifier') or data.get('primary_token_id')
    token_ids = data.get('token_identifiers') or data.get('token_ids', [])

    if not primary_id or not token_ids:
        return jsonify({'success': False, 'message': 'Missing primary_identifier or token_identifiers'}), 400

    if isinstance(token_ids, str):
        token_ids = [token_ids]

    if primary_id in token_ids:
        token_ids = [t for t in token_ids if t != primary_id]

    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_token_group_mappings_table(cursor)
        for tid in token_ids:
            cursor.execute("""
                INSERT INTO token_group_mappings (token_identifier, primary_identifier)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE primary_identifier = VALUES(primary_identifier)
            """, (tid, primary_id))
        conn.commit()
        return jsonify({
            'success': True,
            'message': 'Merge successful',
            'primary_identifier': primary_id,
            'token_identifiers': token_ids
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@token_groups_bp.route('/api/token-groups/unmerge', methods=['POST'])
def api_unmerge_tokens():
    """API hủy gộp nhóm: xóa mapping của primary_identifier hoặc token_identifiers"""
    data = request.get_json() or {}
    primary_id = data.get('primary_identifier') or data.get('primary_token_id')
    token_ids = data.get('token_identifiers') or data.get('token_ids')

    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_token_group_mappings_table(cursor)
        if primary_id:
            cursor.execute("DELETE FROM token_group_mappings WHERE primary_identifier = %s", (primary_id,))
        elif token_ids:
            if isinstance(token_ids, str):
                token_ids = [token_ids]
            cursor.execute("DELETE FROM token_group_mappings WHERE token_identifier IN (%s)" %
                           ','.join(['%s'] * len(token_ids)), tuple(token_ids))
        else:
            return jsonify({'success': False, 'message': 'Missing primary_identifier or token_identifiers'}), 400

        conn.commit()
        return jsonify({'success': True, 'message': 'Unmerge successful'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@token_groups_bp.route('/api/token-groups/list', methods=['GET'])
def api_list_groups():
    """Lấy danh sách các nhóm mapping token hiện tại"""
    conn = get_connection()
    cursor = conn.cursor()
    ensure_token_group_mappings_table(cursor)
    cursor.execute("""
        SELECT primary_identifier, GROUP_CONCAT(token_identifier) AS sub_tokens
        FROM token_group_mappings
        GROUP BY primary_identifier
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    result = []
    for row in rows:
        subs = row[1].split(',') if row[1] else []
        result.append({
            'primary_identifier': row[0],
            'sub_tokens': subs
        })
    return jsonify({'success': True, 'groups': result})

@token_groups_bp.route('/api/save-note', methods=['POST'])
def api_save_note():
    """API lưu hoặc cập nhật User Note hỗ trợ AJAX switch variant"""
    data = request.get_json() or {}
    chain = data.get('chain')
    contract_address = data.get('contract_address')
    symbol = data.get('symbol', '') or ''
    note_text = data.get('user_note', '').strip()

    if not chain or not contract_address:
        return jsonify({'success': False, 'message': 'Missing chain or contract_address'}), 400

    conn = get_connection()
    cursor = conn.cursor()
    vietnam_now = datetime.now(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute("""
        INSERT INTO user_note (chain, contract_address, symbol, user_note, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE user_note = VALUES(user_note), symbol = VALUES(symbol), updated_at = VALUES(updated_at)
    """, (chain, contract_address, symbol, note_text, vietnam_now, vietnam_now))

    cursor.execute("SELECT id FROM user_note WHERE chain = %s AND contract_address = %s", (chain, contract_address))
    res = cursor.fetchone()
    note_id = res[0] if res else None

    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'id': note_id,
        'message': 'Note saved successfully',
        'chain': chain,
        'contract_address': contract_address,
        'user_note': note_text
    })

@token_groups_bp.route('/api/token-groups/save-all', methods=['POST'])
def api_save_all_token_group():
    """
    API Backend refactor lưu nhóm Token chuẩn hóa vào 2 bảng:
    1. token_groups: (group_id, primary_symbol, primary_identifier) -> Chỉ lưu 1 dòng duy nhất cho Token Chính
    2. token_group_members: (id, group_id, symbol, token_identifier) -> Chỉ lưu các Token Con
    3. user_note / pool_notes: Cập nhật note nếu user_note có dữ liệu, giữ nguyên nếu rỗng.
    """
    data = request.get_json() or {}

    selected_id = (data.get('selected_identifier') or '').strip()
    user_note = data.get('user_note', '')
    variants = data.get('variants') or []

    selected_chain = (data.get('selected_chain') or '').strip()
    selected_address = (data.get('selected_address') or '').strip()
    selected_symbol = (data.get('selected_symbol') or '').strip()

    if not selected_id:
        return jsonify({'success': False, 'message': 'Missing selected_identifier'}), 400

    if (not selected_chain or not selected_address) and ':' in selected_id:
        parts = selected_id.split(':', 1)
        selected_chain = selected_chain or parts[0]
        selected_address = selected_address or parts[1]

    # Thu thập mảng tất cả variant_identifiers từ danh sách variants
    variant_ids = []
    for v in variants:
        if isinstance(v, dict) and v.get('identifier'):
            variant_ids.append(v['identifier'].strip())
        elif isinstance(v, str):
            variant_ids.append(v.strip())

    if selected_id not in variant_ids:
        variant_ids.append(selected_id)

    conn = get_connection()
    cursor = conn.cursor()

    try:
        from utils.token_aggregator import ensure_token_groups_tables
        ensure_token_groups_tables(cursor)

        # 1. LÀM SẠCH CẤU TRÚC NHÓM CŨ
        if variant_ids:
            format_strings = ','.join(['%s'] * len(variant_ids))
            
            cursor.execute(f"""
                DELETE FROM token_group_members
                WHERE token_identifier IN ({format_strings})
                   OR group_id IN (SELECT group_id FROM token_groups WHERE primary_identifier IN ({format_strings}))
            """, tuple(variant_ids) + tuple(variant_ids))

            cursor.execute(f"""
                DELETE FROM token_groups
                WHERE primary_identifier IN ({format_strings})
            """, tuple(variant_ids))

        # 2. LƯU TOKEN CHÍNH VÀO BẢNG token_groups (1 DÒNG DUY NHẤT)
        cursor.execute("""
            INSERT INTO token_groups (primary_identifier, primary_symbol)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE primary_symbol = VALUES(primary_symbol)
        """, (selected_id, selected_symbol))

        cursor.execute("SELECT group_id FROM token_groups WHERE primary_identifier = %s", (selected_id,))
        group_row = cursor.fetchone()
        if not group_row:
            raise Exception("Failed to create or retrieve token group")
        
        group_id = group_row[0]

        # 3. LƯU CÁC TOKEN CON VÀO BẢNG token_group_members (BỎ QUA TOKEN CHÍNH)
        for v in variants:
            if isinstance(v, dict):
                v_id = (v.get('identifier') or '').strip()
                v_sym = (v.get('symbol') or '').strip()
            else:
                v_id = str(v).strip()
                v_sym = ''

            # NẾU v_id là Token Chính (selected_id) hoặc rỗng -> BỎ QUA, không lưu vào members
            if not v_id or v_id == selected_id:
                continue

            cursor.execute("""
                INSERT INTO token_group_members (group_id, token_identifier, symbol)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    group_id = VALUES(group_id),
                    symbol = VALUES(symbol)
            """, (group_id, v_id, v_sym))

        # 4. XỬ LÝ USER NOTE (user_note / pool_notes)
        note_saved = False
        note_id = None
        clean_note = user_note.strip() if user_note else ""

        if clean_note != "":
            vietnam_now = datetime.now(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S')

            cursor.execute("""
                INSERT INTO user_note (chain, contract_address, symbol, user_note, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    user_note = VALUES(user_note), 
                    symbol = VALUES(symbol), 
                    updated_at = VALUES(updated_at)
            """, (selected_chain, selected_address, selected_symbol, clean_note, vietnam_now, vietnam_now))

            cursor.execute(
                "SELECT id FROM user_note WHERE chain = %s AND contract_address = %s",
                (selected_chain, selected_address)
            )
            res = cursor.fetchone()
            if res:
                note_id = res[0]
            note_saved = True

        conn.commit()

        return jsonify({
            'success': True,
            'message': 'Saved normalized token group and note successfully',
            'group_id': group_id,
            'selected_identifier': selected_id,
            'note_saved': note_saved,
            'note_id': note_id
        })

    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

