const { Connection, VersionedMessage, VersionedTransaction, clusterApiUrl, Keypair } = solanaWeb3;

// --- Quản lý Trạng thái ---
let state = {
    wallet: null,       // Địa chỉ ví người dùng
    poolContext: null,  // Dữ liệu Pool (Token, Best Position, Decimals...)
    currentPlan: null,  // Kế hoạch Mint hiện tại (Swap TX, Mint TX...)
    isCalculating: false,
    currentSlippage: 50
};

// --- DOM Elements ---
const UI = {
    poolInput: document.getElementById('poolAddress'),
    btnConnect: document.getElementById('btnConnect'),
    connectText: document.getElementById('connectText'),
    connectSpinner: document.getElementById('connectSpinner'),
    walletInfo: document.getElementById('walletInfo'),
    mainInterface: document.getElementById('mainInterface'),
    tokenPairDisplay: document.getElementById('tokenPairDisplay'),
    rangeDisplay: document.getElementById('rangeDisplay'),
    currentTick: document.getElementById('currentTick'),
    liquidity: document.getElementById('liquidity'),
    liquidityUSD: document.getElementById('liquidityUSD'),
    priceImpact: document.getElementById('priceImpact'),

    slider: document.getElementById('multiplierSlider'),
    badge: document.getElementById('multiplierBadge'),
    estShare: document.getElementById('estShare'),
    estLiquidity: document.getElementById('estLiquidity'),
    lblToken0: document.getElementById('lblToken0'),
    lblToken1: document.getElementById('lblToken1'),
    lblLiquidityUSD: document.getElementById('lblLiquidityUSD'),
    valToken0: document.getElementById('valToken0'),
    valToken1: document.getElementById('valToken1'),
    balToken0: document.getElementById('balToken0'),
    balToken1: document.getElementById('balToken1'),

    btnExecute: document.getElementById('btnExecute'),
    actionWarning: document.getElementById('actionWarning'),
    actionText: document.getElementById('actionText'),
    spinner: document.getElementById('spinner'),
    slippageButtons: document.querySelectorAll('[data-slippage]'),
    customSlippageInput: document.getElementById('customSlippage'),

    routeDex: document.getElementById('routeDex')
};

// const RPC_URL = "https://dawn-blissful-pallet.solana-mainnet.quiknode.pro/a2995d002f97f0eb9165a1d8ce906d2ce626aa85/";
const RPC_URL = "https://hardworking-dark-telescope.solana-mainnet.quiknode.pro/547aae6632a0960cd2c3d93cffed3ab52d15d4a1/"; // QuickNode RPC (có thể thay bằng RPC khác nếu cần)

// ============================================
// 1. KHỞI TẠO ỨNG DỤNG (Load Pool & Check Wallet)
// ============================================

window.addEventListener('load', async () => {
    // Mặc định HTML đã hiện "Đang tải pool...", ta hiện thêm spinner
    UI.connectSpinner.classList.remove("hidden");
    UI.btnConnect.disabled = true; // Khóa nút trong lúc đang tải

    // Chạy song song: Kiểm tra ví (Silent) VÀ Tải dữ liệu Pool
    await Promise.all([
        checkWalletConnection(),
        initPoolData()
    ]);
});

// Hàm kiểm tra ví thầm lặng (Silent Connect)
async function checkWalletConnection() {
    try {
        // Đợi nhẹ để window.solana inject
        await new Promise(r => setTimeout(r, 500));

        if (window.solana && window.solana.isPhantom) {
            // onlyIfTrusted: true -> Không hiện popup, chỉ check nếu đã từng connect
            const resp = await window.solana.connect({ onlyIfTrusted: true });
            if (resp && resp.publicKey) {
                console.log("🔄 Auto-detected wallet:", resp.publicKey.toString());
                handleWalletConnected(resp.publicKey.toString());
            } else {
                console.log("Wallet not trusted yet (User needs to connect manually for Minting).");
                // Không làm gì cả, cứ để người dùng xem data pool trước
            }
        }
    } catch (err) {
        console.log("Auto-connect check failed:", err.message);
    }
}

// Xử lý khi ví được kết nối thành công
function handleWalletConnected(walletAddress) {
    state.wallet = walletAddress;

    // Cập nhật UI nhỏ góc trên (nếu cần) hoặc thông báo
    UI.walletInfo.innerText = `Ví: ${walletAddress.slice(0, 6)}...${walletAddress.slice(-4)}`;
    UI.walletInfo.classList.remove("hidden");

    // Nếu nút đang hiển thị trạng thái kết nối, chuyển sang đã kết nối
    // Nhưng nếu đang tải pool, ta ưu tiên giữ trạng thái loading của pool
}

// Hàm kết nối thủ công (dành cho nút phụ hoặc khi user bấm Mint mà chưa connect)
async function manualConnectWallet() {
    try {
        if (!window.solana || !window.solana.isPhantom) {
            alert('Please install Phantom Wallet!');
            window.open('https://phantom.app/', '_blank');
            return null;
        }
        const resp = await window.solana.connect();
        const wallet = resp.publicKey.toString();
        handleWalletConnected(wallet);
        return wallet;
    } catch (err) {
        console.error("Manual connect error:", err);
        showToast("❌ Wallet connection failed", "error");
        return null;
    }
}

async function fetchWalletBalances() {
    if (!state.wallet || !state.poolContext) return;

    try {
        const connection = new solanaWeb3.Connection(RPC_URL, "confirmed");
        const walletPubkey = new solanaWeb3.PublicKey(state.wallet);
        const meta = state.poolContext.token_metadata;

        // Định nghĩa hàm lấy số dư từng token
        const getBalance = async (mint) => {
            // Check Native SOL
            if (mint === "So11111111111111111111111111111111111111112") {
                const bal = await connection.getBalance(walletPubkey);
                return bal / 1e9;
            }
            // Check SPL Token
            else {
                const mintPubkey = new solanaWeb3.PublicKey(mint);
                const tokenAccounts = await connection.getParsedTokenAccountsByOwner(walletPubkey, { mint: mintPubkey });
                if (tokenAccounts.value.length > 0) {
                    return tokenAccounts.value[0].account.data.parsed.info.tokenAmount.uiAmount;
                }
                return 0;
            }
        };

        // Chạy song song
        const [bal0, bal1] = await Promise.all([
            getBalance(meta.token0),
            getBalance(meta.token1)
        ]);

        // Cập nhật UI
        if (UI.balToken0) UI.balToken0.innerText = formatTokenAmount(bal0);
        if (UI.balToken1) UI.balToken1.innerText = formatTokenAmount(bal1);

    } catch (err) {
        console.error("Lỗi lấy số dư:", err);
    }
}

// ============================================
// 2. MODULE INIT POOL (Tải dữ liệu từ Backend)
// ============================================
async function initPoolData() {
    const poolAddr = UI.poolInput.value.trim();

    // Nếu không có pool address, reset nút về trạng thái chờ nhập
    if (!poolAddr) {
        UI.connectText.innerText = "Waiting for Pool Address...";
        UI.connectSpinner.classList.add("hidden");
        UI.btnConnect.disabled = false;
        return;
    }

    try {
        // Gọi API Backend
        const res = await fetch('/api/mint/init', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pool_address: poolAddr })
        });
        const json = await res.json();

        if (json.status !== 'success') throw new Error(json.message);

        state.poolContext = json.data;
        renderInitData();

        if (state.wallet) fetchWalletBalances();

        // Tính toán kế hoạch mẫu (x1.0) để hiển thị số liệu
        // Lưu ý: Lúc này có thể chưa có wallet (state.wallet = null)
        // Backend vẫn tính được Reward Share/Amount, chỉ không tạo được Transaction Swap
        await calculatePlan(1.0);

        // Update UI thành công
        UI.mainInterface.classList.remove('hidden');
        UI.connectText.innerText = "Loaded Pool Data ✅";
        UI.connectSpinner.classList.add("hidden");
        UI.btnConnect.classList.replace("btn-primary", "btn-success");
        // Giữ disabled hoặc enable tùy ý, ở đây ta cứ disable để user tập trung vào phần dưới
        UI.btnConnect.disabled = true;

        showToast("✅ Pool data loaded successfully!", "success");

    } catch (err) {
        console.error("Init Error:", err);
        showToast(`❌ Error loading Pool: ${err.message}`, 'error');

        // Reset nút để user thử lại
        UI.connectText.innerText = "Try again (Load failed)";
        UI.connectSpinner.classList.add("hidden");
        UI.btnConnect.disabled = false;

        // Gán sự kiện click để retry (đã xử lý trong event listener chung ở trên hoặc gán trực tiếp)
        UI.btnConnect.onclick = async () => {
            // Reset UI về trạng thái loading trước khi gọi lại
            UI.connectText.innerText = "Loading pool & Select Range...";
            UI.connectSpinner.classList.remove("hidden");
            UI.btnConnect.disabled = true;
            await initPoolData();
        };
    }
}

function tick_to_price(tick, decimals_mint_0, decimals_mint_1) {
    return (1.0001 ** tick) * (10 ** (decimals_mint_0 - decimals_mint_1));
}

function renderInitData() {
    const ctx = state.poolContext;
    const meta = ctx.token_metadata;
    const pos = ctx.best_position;
    const poolInfo = ctx.pool_info;
    const mint0Decimals = meta.decimals0;
    const mint1Decimals = meta.decimals1;
    const minPrice = tick_to_price(pos.tick_low, mint0Decimals, mint1Decimals);
    const maxPrice = tick_to_price(pos.tick_up, mint0Decimals, mint1Decimals);
    const currentPrice = tick_to_price(poolInfo.tick_current, mint0Decimals, mint1Decimals);

    UI.tokenPairDisplay.innerText = `${meta.symbol0} / ${meta.symbol1} ($${pos.token0_price.toFixed(2)} / $${pos.token1_price.toFixed(2)})`;
    UI.rangeDisplay.innerText = `[ Tick: ${minPrice} ↔ ${maxPrice} ]`;
    UI.liquidity.innerText = `${Intl.NumberFormat().format(pos.liquidity)}`;
    UI.liquidityUSD.innerText = `$${pos.total_value.toFixed(2)}`;
    UI.currentTick.innerText = `${currentPrice}`;
    UI.lblToken0.innerText = meta.symbol0;
    UI.lblToken1.innerText = meta.symbol1;
}

// ============================================
// 3. MODULE CALCULATE (Cập nhật Live)
// ============================================
let debounceTimer;
UI.slider.addEventListener('input', (e) => {
    const val = parseFloat(e.target.value);
    UI.badge.innerText = `x${val.toFixed(1)}`;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => calculatePlan(val), 300);
});

// --- XỬ LÝ SLIPPAGE ---
UI.slippageButtons.forEach(btn => {
    btn.addEventListener('click', (e) => {
        // Update UI active state
        UI.slippageButtons.forEach(b => b.classList.remove('btn-active', 'btn-primary'));
        e.target.classList.add('btn-active', 'btn-primary');

        // Update state & Recalculate
        const val = parseInt(e.target.dataset.slippage);
        state.currentSlippage = val;

        // Gọi lại calculatePlan ngay lập tức để cập nhật lệnh Swap với slippage mới
        const multiplier = parseFloat(UI.slider.value);
        calculatePlan(multiplier);

        showToast(`Đã chỉnh Slippage: ${val / 100}%`, 'info');
    });
});

// 2. Xử lý input custom (Debounce)
let slippageDebounce;
UI.customSlippageInput.addEventListener('input', (e) => {
    clearTimeout(slippageDebounce);

    let val = parseFloat(e.target.value);

    slippageDebounce = setTimeout(() => {
        if (!isNaN(val) && val > 0) {
            UI.slippageButtons.forEach(b => b.classList.remove('btn-active', 'btn-primary'));

            // Chuyển sang BPS và đảm bảo tối thiểu là 1 BPS
            let bps = Math.round(val * 100);

            if (val > 0 && bps === 0) {
                showToast(`Cảnh báo: ${val}% là quá nhỏ, hệ thống sẽ làm tròn về 0%`, 'warning');
            }

            state.currentSlippage = bps;

            if (state.poolContext) {
                const multiplier = parseFloat(UI.slider.value);
                calculatePlan(multiplier);
            }

            showToast(`Slippage: ${val}%`, 'info');
        }
    }, 500);
});

async function calculatePlan(multiplier) {
    if (!state.poolContext) return;

    // Nếu chưa có wallet, gửi address dummy để backend vẫn tính toán được logic
    // (nhưng phần swap tx sẽ không chính xác về số dư)
    const walletToSend = state.wallet || "11111111111111111111111111111111";

    state.isCalculating = true;
    UI.btnExecute.disabled = true;
    UI.estShare.classList.add('opacity-50');

    try {
        const res = await fetch('/api/mint/calculate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_wallet: walletToSend,
                multiplier: multiplier,
                context_data: state.poolContext,
                slippage_bps: state.currentSlippage
            })
        });
        const json = await res.json();
        if (json.status !== 'success') throw new Error(json.message);

        state.currentPlan = json.data;
        renderPlan(json.data);
    } catch (err) {
        console.error("Calc Error:", err);
        // Không toast lỗi tính toán liên tục để tránh spam UI
    } finally {
        state.isCalculating = false;
        UI.estShare.classList.remove('opacity-50');
    }
}

function renderPlan(plan) {

    // 2. Logic Hiển thị Cảnh báo & Nút bấm (Tích lũy thông báo)
    const swaps = plan.actions?.swaps || [];
    const canMint = plan.actions?.can_mint;
    const hasError = plan.summary?.error || swaps.some(s => s.type === 'ERROR');

    // Mảng chứa các thông báo sẽ hiển thị
    let alertMessages = [];
    let alertType = "hidden"; // Mặc định ẩn
    let btnText = "";
    let btnDisabled = false;
    let btnAction = null;

    // 1. Cập nhật Số liệu
    if (plan.summary) {
        if (typeof plan.summary.estimated_reward_share === 'number') {
            UI.estShare.innerText = `${plan.summary.estimated_reward_share.toFixed(4)}%`;
        }
        if (typeof plan.summary.liquidity_minted === 'number') {
            UI.estLiquidity.innerText = new Intl.NumberFormat().format(plan.summary.liquidity_minted);
        }
    }

    if (plan.requirements) {
        UI.valToken0.innerText = formatTokenAmount(plan.requirements.token0.amount);
        UI.valToken1.innerText = formatTokenAmount(plan.requirements.token1.amount);

        // Tính Liquidity USD (nếu có giá từ context)
        const ctx = state.poolContext;
        const pos = ctx.best_position;
        // Kiểm tra xem token_price có tồn tại không trước khi tính
        if (pos && pos.token0_price !== undefined && pos.token1_price !== undefined) {
            const usdVal = plan.requirements.token0.amount * pos.token0_price + plan.requirements.token1.amount * pos.token1_price;
            if (UI.lblLiquidityUSD) UI.lblLiquidityUSD.innerText = `Liquidity Minted(USD): $${usdVal.toFixed(2)}`;
        }
    }

    if (plan.actions) {
        if (typeof plan.actions.price_impact === 'number') {
            if (parseFloat(plan.actions.price_impact) < 1.0) {
                UI.priceImpact.innerText = `⚡ Price Impact: ${plan.actions.price_impact.toFixed(8)}%`;
                UI.priceImpact.classList.add('text-success');
            }
            else if (parseFloat(plan.actions.price_impact) > 5.0) {
                UI.priceImpact.innerText = `⚡ Price Impact: ${plan.actions.price_impact.toFixed(8)}%`;
                UI.priceImpact.classList.add('text-error');
            }
            else {
                UI.priceImpact.innerText = `⚡ Price Impact: ${plan.actions.price_impact.toFixed(8)}%`;
                UI.priceImpact.classList.add('text-warning');
            }
        }
    }

    if (swaps && swaps[0].route) {
        const route = swaps[0].route;
        const routeStep = swaps[0].route_step;
        UI.routeDex.innerText = `📍 Route: ${route}`;
    } else {
        UI.routeDex.innerText = "📍 Route: Unknown Route";
    }

    if (state.wallet) fetchWalletBalances();

    // --- CHECK 0: SELF-COPY WARNING (MỚI) ---
    if (plan.summary && plan.summary.self_copy_warning && plan.summary.self_copy_warning.is_own) {
        alertMessages.push(`
            <div class="mb-2 mb-4">
                <div class="font-bold text-yellow-800">⚠️ SEFL-COPY WARNING</div>
                <div class="text-sm text-yellow-800">${plan.summary.self_copy_warning.message}</div>
            </div>
        `);
        // Không block nút Mint, chỉ hiện cảnh báo
        if (alertType === "hidden") alertType = "alert-warning";
    }

    // --- CHECK 1: RANGE SAFETY (Luôn kiểm tra) ---
    if (plan.summary && plan.summary.range_safety && plan.summary.range_safety.is_safe === false) {
        const msg = plan.summary.range_safety.message || "Range rủi ro cao!";
        alertMessages.push(`
            <div class="mb-2 mb-4">
                <div class="font-bold text-yellow-800">⚠️ RISK WARNING</div>
                <div class="text-sm text-yellow-800">${msg}</div>
            </div>
        `);
        alertType = "alert-warning"; // Ít nhất là warning
    }

    // --- CHECK 2: WALLET ---
    if (!state.wallet) {
        btnText = "🔌 Connect wallet to continue";
        btnDisabled = false;
        btnAction = async () => {
            const connected = await manualConnectWallet();
            if (connected) calculatePlan(parseFloat(UI.slider.value));
        };
    }
    // --- CHECK 3: ERRORS (Critical) ---
    else if (hasError) {
        let errorMsg = plan.summary?.error || "Unknown error";
        const swapError = swaps.find(s => s.type === 'ERROR');
        if (swapError) errorMsg = swapError.description;

        alertMessages.push(`
            <div class="mb-2 border-black/10 pt-2">
                <div class="font-bold text-red-800">❌ CANNOT BE DONE</div>
                <div class="text-sm text-red-800">${errorMsg}</div>
            </div>
        `);

        alertType = "alert-error"; // Nâng cấp lên error (đỏ)
        btnText = "❌ Insufficient Balance to Swap/Mint";
        btnDisabled = true;
    }
    // --- CHECK 4: SWAPS (Info) ---
    else if (swaps.length > 0) {
        const swapMsg = swaps[0].description;
        alertMessages.push(`
            <div class="mb-2 border-black/10 pt-2">
                <div class="font-bold text-blue-800">🔄 NEEDS AUTOMATIC BALANCE</div>
                <div class="text-sm text-blue-800">${swapMsg}</div>
            </div>
        `);

        // Nếu chưa có error, set là warning/info
        if (alertType === "hidden") alertType = "alert-warning";

        btnText = `🔄 Swap & Mint (x${plan.summary.multiplier})`;
        btnDisabled = false;
        btnAction = executeTransactionFlow;
    }
    // --- CHECK 5: NORMAL MINT ---
    else {
        btnText = `✅ Mint Position (x${plan.summary.multiplier})`;
        btnDisabled = !canMint;
        btnAction = executeTransactionFlow;
    }

    // --- RENDER UI ---
    // Reset classes
    UI.actionWarning.className = "alert mt-4 shadow-lg";

    if (alertMessages.length > 0) {
        UI.actionWarning.classList.add(alertType); // Thêm class màu (warning/error)
        UI.actionWarning.classList.remove('hidden');
        UI.actionText.innerHTML = alertMessages.join(""); // Nối các thông báo lại
    } else {
        UI.actionWarning.classList.add('hidden');
    }

    UI.btnExecute.innerText = btnText;
    UI.btnExecute.disabled = btnDisabled;
    if (btnAction) UI.btnExecute.onclick = btnAction;
}


// ============================================
// 4. MODULE EXECUTE (Ký Transaction)
// ============================================
async function executeTransactionFlow() {
    if (!state.wallet) {
        // Fallback: nếu bằng cách nào đó hàm này được gọi mà chưa có ví
        await connectWallet();
        return;
    }
    if (!state.currentPlan) return;

    setLoading(true, "Processing transaction...");
    const actions = state.currentPlan.actions;

    try {
        const connection = new solanaWeb3.Connection(RPC_URL, "confirmed");

        // BƯỚC 4.1: Ký lệnh Swap
        if (actions.swaps && actions.swaps.length > 0) {
            for (const swap of actions.swaps) {
                // Hiển thị info slippage đang dùng để user biết
                showToast(`Đang ký Swap (Slippage: ${state.currentSlippage / 100}%)...`, 'info');

                const txid = await signAndSendBase64(swap.tx_base64, connection);
                showToast(`Swap đã gửi! TX: ${txid.slice(0, 8)}...`, 'success');
                await connection.confirmTransaction(txid, "confirmed");
                await new Promise(r => setTimeout(r, 2000));
                fetchWalletBalances();
            }
        }

        // BƯỚC 4.2: Ký lệnh Mint
        if (actions.can_mint && actions.mint_tx) {
            showToast("Sign & Send Mint Position transaction...", "info");

            // Xử lý Mint TX (có thể là object hoặc string tùy backend trả về)
            let mintTxBase64 = typeof actions.mint_tx === 'string' ? actions.mint_tx : actions.mint_tx.tx_base64;

            // === FIX: Refresh Blockhash cho Mint TX ===
            // Vì Mint TX được tạo từ lúc calculate (có thể cách đây vài phút), blockhash đã cũ.
            // Ta cần decode, thay blockhash mới nhất, rồi mới ký.
            try {
                const txBuffer = Buffer.from(mintTxBase64, 'base64');
                const transaction = solanaWeb3.VersionedTransaction.deserialize(txBuffer);

                // Lấy Blockhash MỚI NHẤT
                const { blockhash } = await connection.getLatestBlockhash("confirmed");
                console.log("Refreshing Mint TX Blockhash:", blockhash);

                // Cập nhật blockhash vào message
                transaction.message.recentBlockhash = blockhash;

                // Serialize lại để gửi hàm signAndSend (hoặc dùng object trực tiếp)
                // Lưu ý: Nếu backend đã partial sign, việc đổi blockhash sẽ làm HỎNG chữ ký đó.
                // Nếu Mint TX cần backend ký (ví dụ tạo NFT mint), thì backend phải ký lại.
                // TUY NHIÊN: Với Position NFT của Pancake, thường User là payer và signer chính.
                // Nếu Backend tạo keypair cho NFT mint và ký trước -> Ta KHÔNG THỂ đổi blockhash ở client được nữa
                // vì chữ ký backend sẽ sai.

                // GIẢI PHÁP: Nếu có swap, sau khi swap xong, GỌI LẠI API CALCULATE để lấy Mint TX mới tinh từ backend.
                if (actions.swaps && actions.swaps.length > 0) {
                    showToast("Refresh new Mint TX (update balance)...", "info");

                    // Gọi lại API calculatePlan để lấy TX mới với blockhash mới và balance mới
                    const multiplier = parseFloat(UI.slider.value);
                    const res = await fetch('/api/mint/calculate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            user_wallet: state.wallet,
                            multiplier: multiplier,
                            context_data: state.poolContext
                        })
                    });
                    const json = await res.json();
                    if (json.status !== 'success') throw new Error("Create new Mint TX Failed: " + json.message);

                    // Cập nhật lại TX Mint mới
                    const newPlan = json.data;
                    if (!newPlan.actions.can_mint) throw new Error("Cannot mint after swap (insufficient conditions).");
                    mintTxBase64 = newPlan.actions.mint_tx.tx_base64;
                }

                // Tiến hành ký và gửi
                showToast("Signing Mint Position transaction...", "info");
                const txid = await signAndSendBase64(mintTxBase64, connection);

                showToast(`🎉 MINT SUCCESS! TX: ${txid.slice(0, 8)}...`, 'success');
                console.log("Mint TXID:", txid);
                setTimeout(() => calculatePlan(1.0), 3000);

            } catch (innerErr) {
                console.error("Refresh Blockhash Error:", innerErr);
                throw new Error("Error update Mint TX: " + innerErr.message);
            }
        }

    } catch (err) {
        console.error("Execution Error:", err);
        if (err.message && err.message.includes("User rejected")) {
            showToast("You have cancelled the transaction.", "warning");
        } else {
            showToast(`Transaction failed: ${err.message}`, 'error');
        }
    } finally {
        setLoading(false);
    }
}

// --- Helper Functions ---
async function signAndSendBase64(txBase64, connection) {
    if (!window.solana) throw new Error("❌ Phantom not found");
    const phantom = window.solana;

    // 1. Deserialize VersionedTransaction từ backend
    const txBytes = Uint8Array.from(atob(txBase64), (c) => c.charCodeAt(0));
    let tx = VersionedTransaction.deserialize(txBytes);

    console.log("📄 Tx from backend:", tx);

    // 2. Phantom ký cho payer (ví user)
    // LƯU Ý: backend đã ký sẵn cho NFT mint.
    // Phantom sẽ thêm/chèn chữ ký cho account tương ứng với publicKey của ví.
    tx = await phantom.signTransaction(tx);

    console.log("✅ After Phantom sign:", tx);

    // 3. (optional) simulate trước khi gửi
    const sim = await connection.simulateTransaction(tx, {
        sigVerify: true,
        commitment: "processed",
    });

    if (sim.value.err) {
        console.log("❌ Simulation error:", sim.value)
        console.error("❌ Simulation error:", sim.value.err);
        throw new Error("Simulation failed, không nên gửi tx này");
    }

    // 4. Gửi tx lên mạng
    const sig = await connection.sendRawTransaction(tx.serialize(), {
        skipPreflight: false,
    });

    console.log("✅ Tx sent:", sig);
    console.log("🔗 https://solscan.io/tx/" + sig);

    const confirmation = await connection.confirmTransaction(sig, "confirmed");
    console.log("📦 Confirmed:", confirmation);

    return sig;
}

function formatTokenAmount(amount) {
    if (amount === 0) return "0.00";
    if (amount < 0.0001) return "< 0.0001";
    return amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

function setLoading(isLoading, text = "Processing transaction...") {
    if (isLoading) {
        UI.spinner.classList.remove('hidden');
        UI.btnExecute.disabled = true;
        if (text) UI.btnExecute.innerHTML = `<span class="loading loading-spinner"></span> ${text}`;
    } else {
        UI.spinner.classList.add('hidden');
        UI.btnExecute.disabled = false;
        // Khôi phục text (sẽ được renderPlan xử lý lại sau khi action xong)
        if (state.currentPlan) renderPlan(state.currentPlan);
    }
}

function showToast(msg, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `alert alert-${type} shadow-lg mb-2`;
    toast.innerHTML = `<span>${msg}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 500);
    }, 4000);
}