/**
 * EVM Semi-Auto Mint Client Service
 * Phụ trách: Kết nối ví, Lấy dữ liệu Pool (gồm cả Competitors), Gọi API Backend, Thực thi Pipeline (Ethers.js v6)
 */

import { ethers } from "https://cdnjs.cloudflare.com/ajax/libs/ethers/6.7.0/ethers.min.js";

const RPC_KEYS = "92ce3193f38d4592a33bba00e65fd936"; // Infura Key (dùng chung cho ETH, Base, Arbitrum)
const PUBLIC_RPCS = {
    "BNB": "https://bsc-dataseed.binance.org",
    "ETH": "https://mainnet.infura.io/v3/" + RPC_KEYS,
    "BAS": "https://base-mainnet.infura.io/v3/" + RPC_KEYS,
    "ARB": "https://arbitrum-mainnet.infura.io/v3/" + RPC_KEYS
};

const CHAIN_IDS = {
    "BNB": 56,
    "BAS": 8453,
    "ARB": 42161,
    "ETH": 1
};

// --- Cấu hình ABIs và Địa chỉ ---
const ERC20_ABI = [
    "function approve(address spender, uint256 amount) public returns (bool)",
    "function allowance(address owner, address spender) public view returns (uint256)",
    "function balanceOf(address account) public view returns (uint256)",
    "function deposit() public payable"
];

const PANCAKE_NPM_ABI = [
    "function mint((address token0, address token1, uint24 fee, int24 tickLower, int24 tickUpper, uint256 amount0Desired, uint256 amount1Desired, uint256 amount0Min, uint256 amount1Min, address recipient, uint256 deadline)) external payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)",
    "function safeTransferFrom(address from, address to, uint256 tokenId, bytes data) external",
    "function approve(address to, uint256 tokenId) external",
    "function getApproved(uint256 tokenId) external view returns (address)"
];

const AERODROME_NPM_ABI = [
    "function mint((address token0, address token1, int24 tickSpacing, int24 tickLower, int24 tickUpper, uint256 amount0Desired, uint256 amount1Desired, uint256 amount0Min, uint256 amount1Min, address recipient, uint256 deadline, uint160 sqrtPriceX96)) external payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)",
    "function approve(address to, uint256 tokenId) external",
    "function getApproved(uint256 tokenId) external view returns (address)"
];

const AERODROME_GAUGE_ABI = [
    "function deposit(uint256 tokenId) external"
];

const NPM_ADDRESSES = {
    "ETH": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    "BNB": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    "BAS": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    "ARB": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
};

const WRAP_TOKENS = [
    "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", // WBNB
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", // WETH (Ethereum)
    "0x4200000000000000000000000000000000000006"  // WETH (Base)
];

// const ZERO_X_ALLOWANCE_HOLDER = "0x000000000022D473030F116dDEE9F6B43aC78BA3";

// --- Quản lý Trạng thái ---
let state = {
    get chain() { return document.getElementById('badgeChain').innerText.trim(); },
    get pool() { return document.getElementById('poolAddress').value.trim(); },
    user: null,
    provider: null,
    signer: null,
    readProvider: null,
    mode: 'manual', // Mặc định là manual để phục vụ chiến lược Copy Trade
    dexType: new URLSearchParams(window.location.search).get('dex_type') || '',
    capital: 10,
    slippageBps: 10,
    tickSpacing: 60,
    pipeline: [],
    isProcessing: false,
    currentTick: 0,
    lastSwapQuotePreview: null,
    quotePreviewTimer: null,
    quotePreviewSeq: 0,
    requiresQuoteReconfirm: false
};

let pipelineRefreshTimer = null;
let pipelineRequestSeq = 0;

const QUOTE_PREVIEW_IDLE_MS = 1800;
const QUOTE_PREVIEW_MAX_AGE_MS = 15000;
const QUOTE_CHANGE_THRESHOLD_BPS = 200;
const SWAP_QUOTE_MAX_AGE_MS = 5000;

// --- DOM Elements ---
const UI = {
    btnConnect: document.getElementById('btnConnect'),
    walletInfo: document.getElementById('walletInfo'),
    btnExecute: document.getElementById('btnExecute'),
    btnText: document.getElementById('btnText'),
    mainSpinner: document.getElementById('mainSpinner'),

    // Stats & Inputs
    slider: document.getElementById('capitalSlider'),
    capBadge: document.getElementById('capitalBadge'),
    tickLow: document.getElementById('tickLow'),
    tickUp: document.getElementById('tickUp'),
    manualInputs: document.getElementById('manualInputs'),

    // Bảng Top Whales
    topPositionsBody: document.getElementById('topPositionsBody'),
    competitorCount: document.getElementById('competitorCount'),

    // Display fields
    estAPR: document.getElementById('estAPR'),
    estShare: document.getElementById('estShare'),
    estLiquidity: document.getElementById('estLiquidity'),
    safetyMargin: document.getElementById('safetyMargin'),
    valT0: document.getElementById('valT0'),
    valT1: document.getElementById('valT1'),
    balT0: document.getElementById('balT0'),
    balT1: document.getElementById('balT1'),

    // Pipeline container
    pipelineContainer: document.getElementById('pipelineContainer'),
    toastContainer: document.getElementById('toast-container'),

    actionWarning: document.getElementById('actionWarning'),
    actionText: document.getElementById('actionText'),
    positionWarning: document.getElementById('positionWarning'),
    positionWarningText: document.getElementById('positionWarningText'),

    priceImpact: document.getElementById('priceImpact'),
    routeDex: document.getElementById('routeDex'),
    pctLower: document.getElementById('pctLower'),
    pctUpper: document.getElementById('pctUpper'),

    t0USD: document.getElementById('t0USD'),
    t1USD: document.getElementById('t1USD'),

    btnMaxT0: document.getElementById('btnMaxT0'),
    btnMaxT1: document.getElementById('btnMaxT1'),
};

// Hàm tính tick spacing dựa vào fee_tier của V3
function setExecuteButtonState(mode, text = null, options = {}) {
    const spinnerVisible = mode === "quoting" || mode === "executing";
    if (UI.mainSpinner) {
        UI.mainSpinner.classList.toggle('hidden', !spinnerVisible);
    }

    const defaults = {
        ready: { disabled: false, text: "START PIPELINE" },
        calculating: { disabled: true, text: "CALCULATING..." },
        quoting: { disabled: true, text: "GETTING ROUTE..." },
        executing: { disabled: true, text: "PROCESSING..." },
        review: { disabled: false, text: "REVIEW ROUTE" },
        error: { disabled: options.disabled ?? false, text: "RESTART PIPELINE" },
        completed: { disabled: true, text: "COMPLETED ✅" }
    };

    const stateConfig = defaults[mode] || defaults.ready;
    UI.btnExecute.disabled = options.disabled ?? stateConfig.disabled;
    UI.btnText.innerText = text || stateConfig.text;
}

function getTickSpacingByFee(feeTier) {
    if (!feeTier) return 60; // Fallback
    const fee = parseInt(feeTier);
    if (fee === 100) return 1;       // 0.01% Fee
    if (fee === 500) return 10;      // 0.05% Fee
    if (fee === 3000) return 60;     // 0.3% Fee
    if (fee === 10000) return 200;   // 1% Fee
    return 60;
}

// Tính phần trăm thay đổi giá giữa 2 tick (V3 formula: price = 1.0001^tick)
function tickPctChange(tickTarget, tickCurrent) {
    const diff = tickTarget - tickCurrent;
    const pct = (Math.pow(1.0001, diff) - 1) * 100;
    return pct;
}

function toFiniteNumber(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
}

function formatPriceValue(value) {
    const num = toFiniteNumber(value);
    if (num === null) return "N/A";
    const maxDigits = Math.abs(num) >= 1 ? 6 : 8;
    return num.toLocaleString(undefined, { maximumFractionDigits: maxDigits });
}

function getDeviationClass(value) {
    const num = toFiniteNumber(value);
    if (num === null) return "opacity-60";
    const absDeviation = Math.abs(num);
    if (absDeviation < 0.3) return "text-success";
    if (absDeviation <= 1) return "text-warning";
    return "text-error";
}

function renderPoolPriceDisplay(poolMeta) {
    const el = document.getElementById('currentPriceDisplay');
    if (!el) return;

    const poolPrice = formatPriceValue(poolMeta?.current_price);
    const marketPrice = toFiniteNumber(poolMeta?.market_price);
    const deviation = toFiniteNumber(poolMeta?.price_deviation_pct);
    const hasMarketPrice = poolMeta?.market_price_status !== "missing" && marketPrice !== null && deviation !== null;

    if (!hasMarketPrice) {
        el.innerHTML = `Pool: ${poolPrice} | <span class="text-error font-bold">Mkt: N/A | &Delta;: N/A</span>`;
        return;
    }

    const sign = deviation > 0 ? '+' : '';
    const deviationClass = getDeviationClass(deviation);
    el.innerHTML = `Pool: ${poolPrice} | Mkt: ${formatPriceValue(marketPrice)} | <span class="${deviationClass} font-bold">&Delta;: ${sign}${deviation.toFixed(2)}%</span>`;
}

// Cập nhật hiển thị % change cho tick inputs
function updateTickPctDisplay() {
    const tickLow = parseInt(UI.tickLow.value);
    const tickUp = parseInt(UI.tickUp.value);
    const ct = state.currentTick;

    if (!isNaN(tickLow) && ct !== 0) {
        const pctL = tickPctChange(tickLow, ct);
        UI.pctLower.textContent = `${pctL >= 0 ? '+' : ''}${pctL.toFixed(2)}%`;
        UI.pctLower.className = `font-bold ${pctL >= 0 ? 'text-success' : 'text-warning'}`;
    } else {
        UI.pctLower.textContent = '--';
    }

    if (!isNaN(tickUp) && ct !== 0) {
        const pctU = tickPctChange(tickUp, ct);
        UI.pctUpper.textContent = `${pctU >= 0 ? '+' : ''}${pctU.toFixed(2)}%`;
        UI.pctUpper.className = `font-bold ${pctU >= 0 ? 'text-success' : 'text-warning'}`;
    } else {
        UI.pctUpper.textContent = '--';
    }
}

// ============================================
// 1. KHỞI TẠO & ĐỒNG BỘ UI
// ============================================

async function init() {
    setupListeners();
    // Luôn hiển thị input nhập tay (Manual Mode)
    UI.manualInputs.classList.remove('hidden');

    // Tự động kết nối ví ngay khi trang load
    await connectWallet();

    // 2. CHỈ KHI ví đã sẵn sàng, mới bắt đầu tải Metadata
    await loadMetadata();
}

function setupListeners() {
    // Lắng nghe thay đổi slider vốn
    document.getElementById('capitalSlider').addEventListener('input', (e) => {
        state.capital = parseFloat(e.target.value);
        document.getElementById('capitalBadge').innerText = `$${state.capital}`;
        schedulePipelineRefresh(500);
    });

    // Lắng nghe thay đổi tick (khi người dùng tự sửa sau khi copy)
    UI.tickLow.addEventListener('input', () => {
        updateTickPctDisplay();
        schedulePipelineRefresh(500);
    });
    UI.tickUp.addEventListener('input', () => {
        updateTickPctDisplay();
        schedulePipelineRefresh(500);
    });

    // Lắng nghe nút Connect (dự phòng nếu người dùng tắt popup auto và muốn connect lại)
    document.getElementById('btnConnect').addEventListener('click', connectWallet);
    UI.btnExecute.addEventListener('click', runExecutionFlow);

    // === TÍCH HỢP SLIPPAGE UI ===
    const slippageButtons = document.querySelectorAll('#slippageGroup button[data-sl]');
    const customSlippageInput = document.getElementById('customSlippage');

    function updateSlippageUI() {
        slippageButtons.forEach(btn => {
            if (parseInt(btn.dataset.sl) === state.slippageBps) {
                btn.classList.add('btn-active', 'btn-primary');
            } else {
                btn.classList.remove('btn-active', 'btn-primary');
            }
        });
        customSlippageInput.value = state.slippageBps / 100;
    }

    // Khởi tạo UI slippage mặc định
    updateSlippageUI();

    // 1. Lắng nghe các nút có sẵn (0.1%, 0.5%, 1.0%)
    slippageButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            state.slippageBps = parseInt(btn.dataset.sl);

            // Cập nhật giao diện
            updateSlippageUI();

            console.log("Slippage đã chuyển sang:", state.slippageBps, "bps");
            showToast("Slippage đã chuyển sang: " + (state.slippageBps / 100) + " %", "success");
            schedulePipelineRefresh(300);
        });
    });

    // 2. Lắng nghe ô nhập Custom (Dành cho Meme Token Tax cao)
    customSlippageInput.addEventListener('input', (e) => {
        const val = parseFloat(e.target.value);
        if (!isNaN(val) && val > 0) {
            // Chuyển % sang BPS (VD: 15.5% -> 1550)
            state.slippageBps = Math.floor(val * 100);

            // Khống chế mức max là 50% (5000 bps) để tránh user gõ nhầm
            if (state.slippageBps > 5000) state.slippageBps = 5000;

            // Cập nhật active cho các nút cố định tương ứng
            slippageButtons.forEach(b => {
                if (parseInt(b.dataset.sl) === state.slippageBps) {
                    b.classList.add('btn-active', 'btn-primary');
                } else {
                    b.classList.remove('btn-active', 'btn-primary');
                }
            });

            console.log("Custom Slippage:", state.slippageBps / 100, "%");
            showToast("Slippage đã chuyển sang: " + state.slippageBps / 100 + " %", "success");
            schedulePipelineRefresh(800); // Đợi user gõ xong mới refresh
        } else {
            slippageButtons.forEach(b => b.classList.remove('btn-active', 'btn-primary'));
        }
    });

    // Tự động format lại khi blur ra ngoài nếu nhập sai hoặc để trống
    customSlippageInput.addEventListener('blur', () => {
        customSlippageInput.value = state.slippageBps / 100;
    });

    // 3. Logic tự làm tròn tick
    function snapToTickSpacing(inputElement) {
        let val = parseInt(inputElement.value);
        if (isNaN(val)) return;

        // Công thức làm tròn tới bội số gần nhất của tickSpacing
        let snapped = Math.round(val / state.tickSpacing) * state.tickSpacing;

        if (val !== snapped) {
            inputElement.value = snapped;
            showToast(`Đã tự động nắn Tick về ${snapped} cho đúng chuẩn Pool!`, "info");
        }
    }

    // Đổi từ 'input' sang 'change' để không làm phiền người dùng lúc đang gõ dở
    UI.tickLow.addEventListener('change', (e) => {
        snapToTickSpacing(e.target);
        schedulePipelineRefresh(500);
    });

    UI.tickUp.addEventListener('change', (e) => {
        snapToTickSpacing(e.target);
        schedulePipelineRefresh(500);
    });

    // --- Logic cho nút Max Asset ---
    const handleMaxClick = (tokenIndex) => {
        if (!state.currentPlan || !state.currentPlan.strategy_analysis) return;
        const analysis = state.currentPlan.strategy_analysis;
        const meta = state.currentPlan.metadata;

        let walletRaw, reqRaw, tokenSymbol;
        if (tokenIndex === 0) {
            walletRaw = state.bal0Raw || 0n;
            reqRaw = BigInt(analysis.amount0_raw || "0");
            tokenSymbol = meta.token0_symbol;
        } else {
            walletRaw = state.bal1Raw || 0n;
            reqRaw = BigInt(analysis.amount1_raw || "0");
            tokenSymbol = meta.token1_symbol;
        }

        if (walletRaw <= 0n) {
            return showToast(`You don't have ${tokenSymbol} in your wallet!`, "warning");
        }

        if (reqRaw <= 0n) {
            return showToast(`${tokenSymbol} is not needed for the current price range!`, "info");
        }

        // Tinh multiplier (dung float de bao toan do chinh xac vi raw > uint256 max)
        const walletFloat = Number(walletRaw);
        const reqFloat = Number(reqRaw);
        const scaleMultiplier = walletFloat / reqFloat;

        let newCapital = state.capital * scaleMultiplier;

        // Mo rong gioi han slider neu vuot qua
        if (newCapital > parseFloat(UI.slider.max)) {
            UI.slider.max = Math.ceil(newCapital * 1.5);
        }

        state.capital = newCapital;
        UI.slider.value = state.capital;
        UI.capBadge.innerText = `$${state.capital.toFixed(2)}`;

        showToast(`Auto set capital to Max ${tokenSymbol}`, "success");
        schedulePipelineRefresh(300);
    };

    if (UI.btnMaxT0) UI.btnMaxT0.addEventListener('click', () => handleMaxClick(0));
    if (UI.btnMaxT1) UI.btnMaxT1.addEventListener('click', () => handleMaxClick(1));
}

// Thay thế hàm connectWallet hiện tại
async function connectWallet() {
    if (!window.ethereum) {
        UI.btnConnect.innerText = "Please install MetaMask";
        return showToast("Please install MetaMask wallet", "warning");
    }

    try {
        state.provider = new ethers.BrowserProvider(window.ethereum, "any");

        // 1. Khởi tạo Read Provider tĩnh dựa trên Chain UI đang chọn
        const rpcUrl = PUBLIC_RPCS[state.chain] || PUBLIC_RPCS["BSC"];
        state.readProvider = new ethers.JsonRpcProvider(rpcUrl);

        const accounts = await state.provider.send("eth_requestAccounts", []);

        if (accounts.length > 0) {
            state.user = accounts[0];
            state.signer = await state.provider.getSigner();

            // 2. ÉP CHUYỂN MẠNG TRÊN METAMASK
            await ensureCorrectNetwork();

            state.signer = await state.provider.getSigner();

            UI.btnConnect.classList.add('hidden');
            UI.walletInfo.innerText = `Wallet: ${state.user.slice(0, 6)}...${state.user.slice(-4)}`;
            UI.walletInfo.classList.remove('hidden');

            // refreshPipeline();
        }
    } catch (error) {
        if (error.code === -32002) {
            showToast("Please open MetaMask and accept the pending connection request!", "info");
        } else {
            console.warn("Error connecting wallet:", error);
            showToast("Failed to connect wallet. Please try again!", "warning");
        }
    }
}

// THÊM HÀM NÀY: Ép chuyển mạng
async function ensureCorrectNetwork() {
    const chainKey = state.chain.toUpperCase();
    const targetChainId = CHAIN_IDS[chainKey];
    if (!targetChainId) return;

    const network = await state.provider.getNetwork();
    if (Number(network.chainId) !== targetChainId) {
        try {
            await window.ethereum.request({
                method: 'wallet_switchEthereumChain',
                params: [{ chainId: ethers.toQuantity(targetChainId) }],
            });

            state.provider = new ethers.BrowserProvider(window.ethereum, "any");
            state.signer = await state.provider.getSigner();
        } catch (switchError) {
            console.error(switchError);
            showToast(`Please switch MetaMask to ${chainKey}`, "warning");
        }
    }
}

// ============================================
// 2. LẤY DỮ LIỆU & RENDER BẢNG COMPETITORS
// ============================================

async function loadMetadata() {
    const url = new URL('/api/v3/pool-metadata', window.location.origin);
    url.searchParams.append('chain', state.chain);
    url.searchParams.append('pool_address', state.pool);
    if (state.dexType) url.searchParams.append('dex_type', state.dexType);

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (data.error || !data.pool_meta) {
            throw new Error(data.msg || data.message || data.error || "Pool metadata unavailable");
        }
        state.dexType = data.pool_meta.dex_type || state.dexType;

        // 1. Cập nhật thông tin cơ bản
        document.getElementById('tokenPairDisplay').innerHTML = data.pool_meta.pair + " (" + data.pool_meta.token1_price + " / " + data.pool_meta.token0_price + ")" || "Unknown Pair";
        renderPoolPriceDisplay(data.pool_meta || {});
        document.getElementById('tickDisplay').innerText = `Tick: ${data.pool_meta.current_tick || 0}`;
        document.getElementById('poolFee').innerText = `${(data.pool_meta.fee_tier || 0) / 10000}% Fee`;
        document.getElementById('totalLiquidity').innerText = data.pool_meta.total_active_l || '0';
        state.currentTick = parseInt(data.pool_meta.current_tick) || 0;
        state.tickSpacing = data.pool_meta.tick_spacing || getTickSpacingByFee(data.pool_meta.fee_tier);

        // Gán step cho input HTML để mũi tên tăng/giảm hoạt động đúng
        UI.tickLow.step = state.tickSpacing;
        UI.tickUp.step = state.tickSpacing;
        console.log(`Pool Fee: ${data.pool_meta.fee_tier} -> Tick Spacing: ${state.tickSpacing}`);

        // 2. Render và Auto-Select
        if (data.pool_meta.competitors && data.pool_meta.competitors.length > 0) {
            renderCompetitors(data.pool_meta.competitors);

            // Kiểm tra vị thế cũ của user
            if (state.user) {
                const userPositions = data.pool_meta.competitors.filter(c =>
                    c.user?.id?.toLowerCase() === state.user.toLowerCase()
                );

                if (userPositions.length > 0) {
                    const tokenIds = userPositions.map(p => `#${p.id}`).join(', ');
                    UI.positionWarning.classList.remove('hidden');
                    UI.positionWarningText.innerHTML = `⚠️ You already have <strong>${userPositions.length} positions</strong> (${tokenIds}) in this pool. Consider before adding more!`;

                    showToast("Detected old positions!", "warning");
                } else {
                    UI.positionWarning.classList.add('hidden'); // Hide if user doesn't have any positions
                }
            }

            // Tự động chọn Tick của Top 1 Whale
            const bestWhale = data.pool_meta.competitors[0];
            UI.tickLow.value = bestWhale.tickLower;
            UI.tickUp.value = bestWhale.tickUpper;
            state.mode = 'manual';

            // CHỈ GỌI REFRESH PIPELINE TẠI ĐÂY SAU KHI MỌI THỨ ĐÃ SẴN SÀNG
            console.log("Metadata loaded. Auto-selected range:", bestWhale.tickLower, "->", bestWhale.tickUpper);
            updateTickPctDisplay();
            await refreshPipeline();

        } else if (data.pool_meta.manual_required || state.dexType === 'aerodrome_v3') {
            UI.topPositionsBody.innerHTML = '<tr><td colspan="7" class="text-center py-6">Manual range required for this pool</td></tr>';
            const defaultRange = data.pool_meta.default_manual_range || [
                (Math.floor(state.currentTick / state.tickSpacing) - 10) * state.tickSpacing,
                (Math.floor(state.currentTick / state.tickSpacing) + 10) * state.tickSpacing
            ];
            UI.tickLow.value = defaultRange[0];
            UI.tickUp.value = defaultRange[1];
            state.mode = 'manual';
            updateTickPctDisplay();
            await refreshPipeline();
        } else {
            // Không có đối thủ, chờ người dùng nhập
            UI.topPositionsBody.innerHTML = '<tr><td colspan="7" class="text-center py-6">No in-range competitors found</td></tr>';
            UI.btnText.innerText = "Enter Tick Range to calculate";
            UI.btnExecute.disabled = true;
        }

    } catch (e) {
        console.error("Metadata load failed", e);
        UI.topPositionsBody.innerHTML = '<tr><td colspan="7" class="text-center py-6 text-error">Load data failed</td></tr>';
    }
}

function renderCompetitors(competitors) {
    const ct = state.currentTick;
    UI.competitorCount.innerText = `${competitors.length} Active`;
    UI.topPositionsBody.innerHTML = competitors.map(c => {
        const ownerStr = c.user?.id ? `${c.user.id.slice(0, 6)}...${c.user.id.slice(-4)}` : 'Unknown';
        const shareStr = c.share_percent ? c.share_percent.toFixed(2) : '0.00';
        const liqStr = parseFloat(c.liquidity).toExponential(2);

        // Calculate % change from current tick
        const pctLower = tickPctChange(c.tickLower, ct);
        const pctUpper = tickPctChange(c.tickUpper, ct);
        const pctLowerStr = `${pctLower >= 0 ? '+' : ''}${pctLower.toFixed(2)}%`;
        const pctUpperStr = `${pctUpper >= 0 ? '+' : ''}${pctUpper.toFixed(2)}%`;
        const lowerColor = pctLower >= 0 ? 'text-success' : 'text-warning';
        const upperColor = pctUpper >= 0 ? 'text-success' : 'text-warning';

        return `
            <tr class="hover">
                <td class="font-mono text-[10px] opacity-70">${ownerStr}</td>
                <td class="font-bold text-primary text-[10px]">#${c.id}</td>
                <td class="font-mono text-xs">[${c.tickLower} \u27a1\ufe0f ${c.tickUpper}]</td>
                <td class="font-mono text-[10px]">
                    <span class="${lowerColor}">${pctLowerStr}</span>
                    <span class="opacity-30"> / </span>
                    <span class="${upperColor}">${pctUpperStr}</span>
                </td>
                <td class="font-mono text-[10px]">${c.liquidity_usd}</td>
                <td class="text-success font-bold text-[10px]">${shareStr}%</td>
                <td class="text-right">
                    <button class="btn btn-xs btn-outline btn-primary btn-copy-range" 
                        data-low="${c.tickLower}" data-up="${c.tickUpper}">
                        Copy Range
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    // Gắn sự kiện "Copy Range"
    document.querySelectorAll('.btn-copy-range').forEach(btn => {
        btn.addEventListener('click', (e) => {
            UI.tickLow.value = e.target.dataset.low;
            UI.tickUp.value = e.target.dataset.up;
            state.mode = 'manual';

            // Highlight effect
            UI.tickLow.classList.add('border-primary', 'bg-primary/10');
            UI.tickUp.classList.add('border-primary', 'bg-primary/10');
            setTimeout(() => {
                UI.tickLow.classList.remove('border-primary', 'bg-primary/10');
                UI.tickUp.classList.remove('border-primary', 'bg-primary/10');
            }, 800);

            // Update % change display
            updateTickPctDisplay();

            // Refresh pipeline
            schedulePipelineRefresh(0);
        });
    });
}

// ============================================
// 3. TẠO KẾ HOẠCH (PIPELINE) TỪ BACKEND
// ============================================

function getCustomRangeForPlan() {
    if (state.mode !== 'manual') return null;
    const range = [parseInt(UI.tickLow.value), parseInt(UI.tickUp.value)];
    if (isNaN(range[0]) || isNaN(range[1])) return null;
    return range;
}

function buildPlanPayload(quoteMode) {
    return {
        chain: state.chain,
        pool: state.pool,
        user: state.user,
        capital: state.capital,
        mode: state.mode,
        custom_range: getCustomRangeForPlan(),
        slippage: state.slippageBps,
        quote_mode: quoteMode,
        dex_type: state.dexType
    };
}

function resetQuotePreviewState(clearDisplay = false) {
    clearTimeout(state.quotePreviewTimer);
    state.quotePreviewTimer = null;
    state.quotePreviewSeq += 1;
    state.lastSwapQuotePreview = null;
    state.requiresQuoteReconfirm = false;
    if (clearDisplay) {
        UI.priceImpact.innerText = "--";
        UI.routeDex.innerText = "Route pending";
    }
}

function formatImpact(value) {
    const num = Number(value);
    return Number.isFinite(num) ? `${num.toFixed(4)}%` : "--";
}

function quoteFromSwapStep(step) {
    if (!step) return null;
    return {
        provider: step.provider,
        route_display: step.route_display,
        price_impact: step.price_impact,
        buy_amount_raw: step.buy_amount_raw,
        sell_amount_raw: step.sell_amount_raw,
        token_in_address: step.token_in_address,
        token_out_address: step.token_out_address,
        quoted_at_ms: step.quoted_at_ms || Date.now()
    };
}

function isQuotePreviewFresh(quote) {
    const quotedAt = Number(quote?.quoted_at_ms);
    return Number.isFinite(quotedAt) && Date.now() - quotedAt <= QUOTE_PREVIEW_MAX_AGE_MS;
}

function renderSwapQuotePreview(quote, message = null, alertClass = 'alert-info') {
    if (!quote) return;
    UI.priceImpact.innerText = formatImpact(quote.price_impact);
    UI.routeDex.innerText = quote.provider ? `Indicative: ${quote.provider}` : "Indicative route";
    UI.actionWarning.className = "alert shadow-sm mb-2";
    UI.actionWarning.classList.remove('hidden');
    UI.actionWarning.classList.add(alertClass);
    const route = quote.route_display || "Route available";
    const text = message || `Indicative route: ${route} | Impact: ${formatImpact(quote.price_impact)} | Updated now`;
    UI.actionText.innerHTML = `<strong>Route preview:</strong> ${text}`;
}

function compareQuotes(preview, fresh) {
    const issues = [];
    const previewImpact = Number(preview?.price_impact);
    const freshImpact = Number(fresh?.price_impact);
    if (Number.isFinite(previewImpact) && Number.isFinite(freshImpact)) {
        const impactIncrease = freshImpact - previewImpact;
        if (impactIncrease > QUOTE_CHANGE_THRESHOLD_BPS / 100) {
            issues.push(`Impact ${previewImpact.toFixed(2)}% -> ${freshImpact.toFixed(2)}%`);
        }
    }

    try {
        const previewBuy = BigInt(preview?.buy_amount_raw || "0");
        const freshBuy = BigInt(fresh?.buy_amount_raw || "0");
        if (previewBuy > 0n && freshBuy < previewBuy) {
            const dropBps = ((previewBuy - freshBuy) * 10000n) / previewBuy;
            if (dropBps > BigInt(QUOTE_CHANGE_THRESHOLD_BPS)) {
                issues.push(`Output down ${(Number(dropBps) / 100).toFixed(2)}%`);
            }
        }
    } catch (e) {
        console.warn("Unable to compare quote output", e);
    }

    return { shouldBlock: issues.length > 0, message: issues.join("; ") };
}

function getCurrentSwapPlan() {
    return state.currentPlan?.swap_step || state.currentPlan?.swap_intent || null;
}

function createRouteReviewError(message) {
    const err = new Error(message);
    err.isRouteReviewRequired = true;
    return err;
}

function getQuoteAgeMs(step) {
    const quotedAt = Number(step?.quoted_at_ms);
    if (!Number.isFinite(quotedAt)) return Number.POSITIVE_INFINITY;
    return Date.now() - quotedAt;
}

async function fetchQuotePreviewForExecutionReview() {
    clearTimeout(state.quotePreviewTimer);
    state.quotePreviewSeq += 1;
    setExecuteButtonState("quoting");
    const res = await fetch('/api/v3/generate-plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPlanPayload("quote_preview"))
    });
    const data = await res.json();
    if (data.swap_quote_preview) {
        state.lastSwapQuotePreview = data.swap_quote_preview;
        state.requiresQuoteReconfirm = true;
        if (state.currentPlan) state.currentPlan.swap_quote_preview = data.swap_quote_preview;
        renderSwapQuotePreview(data.swap_quote_preview, "Fresh route loaded. Review and click Execute again.", "alert-warning");
        setExecuteButtonState("review");
        return data.swap_quote_preview;
    }
    setExecuteButtonState("error", "ROUTE ERROR");
    return null;
}

async function ensureRoutePreviewForExecution() {
    if (!getCurrentSwapPlan()) return true;
    if (isQuotePreviewFresh(state.lastSwapQuotePreview)) return true;
    await fetchQuotePreviewForExecutionReview();
    return false;
}

async function fetchFullSwapStepForExecution() {
    clearTimeout(pipelineRefreshTimer);
    clearTimeout(state.quotePreviewTimer);
    state.quotePreviewSeq += 1;
    setExecuteButtonState("quoting");
    const fullPlan = await refreshPipeline("full", { manageButton: false, renderPipeline: false });
    if (!fullPlan || fullPlan.error || !fullPlan.swap_step?.tx) {
        setExecuteButtonState("error", "ROUTE ERROR");
        throw new Error(fullPlan?.error?.description || "Unable to fetch executable swap route.");
    }
    setExecuteButtonState("executing");
    return fullPlan.swap_step;
}

function guardRouteChangeForExecution(swapStep) {
    const freshQuote = quoteFromSwapStep(swapStep);
    const previewQuote = state.lastSwapQuotePreview;
    if (!isQuotePreviewFresh(previewQuote)) {
        state.lastSwapQuotePreview = freshQuote;
        state.requiresQuoteReconfirm = true;
        renderSwapQuotePreview(freshQuote, "Fresh route loaded. Review and click Execute again.", "alert-warning");
        setExecuteButtonState("review");
        throw createRouteReviewError("Fresh route requires review.");
    }

    const comparison = compareQuotes(previewQuote, freshQuote);
    if (comparison.shouldBlock) {
        state.lastSwapQuotePreview = freshQuote;
        state.requiresQuoteReconfirm = true;
        renderSwapQuotePreview(freshQuote, `Route changed: ${comparison.message}. Review and click Execute again.`, "alert-warning");
        setExecuteButtonState("review");
        throw createRouteReviewError(`Route changed: ${comparison.message}`);
    }

    state.lastSwapQuotePreview = freshQuote;
    state.requiresQuoteReconfirm = false;
}

function scheduleQuotePreview() {
    clearTimeout(state.quotePreviewTimer);
    if (!state.currentPlan?.swap_intent || state.isProcessing) return;
    const seq = ++state.quotePreviewSeq;
    state.quotePreviewTimer = setTimeout(() => fetchQuotePreview(seq), QUOTE_PREVIEW_IDLE_MS);
}

async function fetchQuotePreview(seq) {
    if (!state.currentPlan?.swap_intent || state.isProcessing) return null;
    try {
        const res = await fetch('/api/v3/generate-plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildPlanPayload("quote_preview"))
        });
        const data = await res.json();
        if (state.isProcessing) return null;
        if (seq !== state.quotePreviewSeq) return null;
        if (data.swap_quote_preview) {
            state.lastSwapQuotePreview = data.swap_quote_preview;
            state.requiresQuoteReconfirm = false;
            if (state.currentPlan) state.currentPlan.swap_quote_preview = data.swap_quote_preview;
            renderSwapQuotePreview(data.swap_quote_preview);
        } else if (data.quote_warning) {
            UI.routeDex.innerText = "Route unavailable";
            UI.actionWarning.className = "alert shadow-sm mb-2";
            UI.actionWarning.classList.remove('hidden');
            UI.actionWarning.classList.add('alert-warning');
            UI.actionText.innerHTML = `<strong>Route preview:</strong> ${data.quote_warning.description || "Unable to fetch indicative route."}`;
        }
        return data;
    } catch (e) {
        if (seq === state.quotePreviewSeq) console.warn("Quote preview error", e);
        return null;
    }
}

async function refreshPipeline(quoteMode = "preview", options = {}) {
    const manageButton = options.manageButton !== false;
    const renderPipeline = options.renderPipeline !== false;
    if (!state.user || !state.pool) return;

    const customRange = state.mode === 'manual' ? [parseInt(UI.tickLow.value), parseInt(UI.tickUp.value)] : null;

    if (state.mode === 'manual' && (isNaN(customRange[0]) || isNaN(customRange[1]))) {
        return;
    }

    const requestSeq = ++pipelineRequestSeq;

    if (manageButton) {
        setExecuteButtonState(quoteMode === "full" ? "quoting" : "calculating");
    }

    try {
        const res = await fetch('/api/v3/generate-plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildPlanPayload(quoteMode))
        });

        const data = await res.json();
        if (requestSeq !== pipelineRequestSeq) return null;

        state.currentPlan = data;
        if (data.metadata?.dex_type) state.dexType = data.metadata.dex_type;

        // Reset trạng thái alert trước khi cập nhật
        UI.actionWarning.className = "alert shadow-sm mb-2";

        // Xử lý cờ lỗi để disable nút Execute, nhưng KHÔNG return để tiếp tục render UI
        let hasError = false;
        if (data.error) {
            UI.actionWarning.classList.remove('hidden');
            UI.actionWarning.classList.add(data.error.description.includes("Balance") ? 'alert-warning' : 'alert-error');
            UI.actionText.innerHTML = `<strong>⚠️ Warning:</strong> ${data.error.description}`;

            showToast(`Warning: ${data.error.description}`, "warning");
            console.log(`Warning: ${data.error.description}`);
            if (manageButton) setExecuteButtonState("error", data.error.description.toLowerCase().includes("balance") ? "INSUFFICIENT BALANCE" : "PLANNING ERROR", { disabled: true });
            // Tùy biến text dựa trên lỗi
            hasError = true;
        } else {
            if (data.swap_intent && !data.swap_step && data.swap_intent.description) {
                UI.priceImpact.innerText = "--";
                UI.routeDex.innerText = data.swap_intent.route_display || "Route pending";

                UI.actionWarning.classList.remove('hidden');
                UI.actionWarning.classList.add('alert-info');
                UI.actionText.innerHTML = `<strong>Route pending:</strong> ${data.swap_intent.description}`;
            } else if (data.swap_step && data.swap_step.description) {
                UI.priceImpact.innerText = parseFloat(data.swap_step.price_impact).toFixed(6) + "%";
                UI.routeDex.innerText = data.swap_step.route_display;

                UI.actionWarning.classList.remove('hidden');
                UI.actionWarning.classList.add('alert-info');
                UI.actionText.innerHTML = `<strong>🔄 Optimization:</strong> ${data.swap_step.description}`;
            } else {
                // Nếu không có lỗi và không cần swap (tỷ lệ token đã hoàn hảo), hiện success
                UI.actionWarning.classList.remove('hidden');
                UI.actionWarning.classList.add('alert-success');
                UI.actionText.innerHTML = `<strong>✅ Ready:</strong> Token ratio is balanced, ready to Mint!`;
            }

            if (manageButton) setExecuteButtonState("ready");
        }

        // --- RENDER GIAO DIỆN (Luôn chạy bất chấp có lỗi hay không) ---

        // 1. Cập nhật thông số Analysis (Dùng ?. để chống crash nếu data bị thiếu)
        if (data.strategy_analysis) {
            const isAerodrome = data.metadata?.dex_type === 'aerodrome_v3';
            UI.estAPR.innerText = isAerodrome ? 'N/A' : `${(data.strategy_analysis.estimated_apr || 0).toFixed(0)}%`;
            UI.estShare.innerText = isAerodrome ? 'N/A' : `${(data.strategy_analysis.share || 0).toFixed(4)}%`;
            UI.estLiquidity.innerText = `${(data.strategy_analysis.liquidity_user || 0).toFixed(0)}`;

            if (UI.safetyMargin && data.strategy_analysis.safety_margin !== undefined) {
                UI.safetyMargin.innerText = `${data.strategy_analysis.safety_margin.toFixed(2)}%`;
            }
        }

        // 2. Render số lượng Token 0 và Token 1 cần thiết
        if (data.metadata && data.strategy_analysis) {
            document.getElementById('lblT0').innerText = data.metadata.token0_symbol || "Token 0";
            document.getElementById('lblT1').innerText = data.metadata.token1_symbol || "Token 1";

            const t0Decimals = data.metadata.token0_decimals || 18;
            const t1Decimals = data.metadata.token1_decimals || 18;

            const amt0Raw = data.strategy_analysis.amount0_raw || "0";
            const amt1Raw = data.strategy_analysis.amount1_raw || "0";

            const amt0Human = ethers.formatUnits(amt0Raw, t0Decimals);
            const amt1Human = ethers.formatUnits(amt1Raw, t1Decimals);

            UI.valT0.innerText = parseFloat(amt0Human).toFixed(4);
            UI.valT1.innerText = parseFloat(amt1Human).toFixed(4);

            const price0 = data.metadata.token0_price;
            const price1 = data.metadata.token1_price;

            UI.t0USD.innerText = `$${(parseFloat(amt0Human) * price0 || 0.00).toFixed(2)}`;
            UI.t1USD.innerText = `$${(parseFloat(amt1Human) * price1 || 0.00).toFixed(2)}`;

            // Kiểm tra số dư ví để người dùng biết mình thiếu bao nhiêu
            checkWalletBalances(data.metadata.token0_address, data.metadata.token1_address, t0Decimals, t1Decimals);
        }

        // 3. Render các bước của Pipeline
        // Chỉ render pipeline nếu Backend trả về đủ metadata và ko bị lỗi chí mạng (thiếu data root)
        if (renderPipeline && data.metadata) {
            renderPipelineUI(data);
        }

        if (quoteMode === "preview") {
            if (data.swap_intent && !data.swap_step && !data.error) {
                scheduleQuotePreview();
            } else {
                resetQuotePreviewState(false);
            }
        }

        return data;

    } catch (e) {
        if (requestSeq !== pipelineRequestSeq) return null;

        console.error("Error refreshing pipeline:", e);
        // Bắt lỗi sập mạng hoặc Backend không phản hồi
        UI.actionWarning.className = "alert shadow-sm mb-2";
        UI.actionWarning.classList.remove('hidden');
        UI.actionWarning.classList.add('alert-error');
        UI.actionText.innerHTML = `<strong>❌ Connection error:</strong> Unable to fetch data from server.`;

        if (manageButton) setExecuteButtonState("error", "NETWORK ERROR", { disabled: true });
        return null;
    }
}

const WRAPPED_NATIVE = [
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", // WBNB (BSC)
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", // WETH (Ethereum)
    "0x4200000000000000000000000000000000000006", // WETH (Base/Optimism)
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"  // WETH (Arbitrum)
];

async function getSafeMemeBalance(tokenAddr, userAddr) {
    const chainName = state.chain ? state.chain.toUpperCase() : "BSC";
    const rpcUrl = PUBLIC_RPCS[chainName];

    if (!rpcUrl) {
        console.warn(`No RPC configured for chain: ${chainName}`);
        return 0n;
    }

    const correctProvider = new ethers.JsonRpcProvider(rpcUrl);

    try {
        // 1. Đọc số dư ERC20 (Wrapped Token / Meme Token)
        const cleanUserAddr = userAddr.toLowerCase().replace("0x", "");
        const data = "0x70a08231" + cleanUserAddr.padStart(64, "0");

        const result = await correctProvider.call({
            to: tokenAddr,
            data: data
        });

        let erc20Balance = 0n;
        if (result && result !== "0x") {
            erc20Balance = BigInt(result);
        }

        // 2. CHECK NATIVE TOKEN: Nếu là WBNB/WETH, cộng thêm số dư Native (BNB/ETH)
        if (WRAPPED_NATIVE.includes(tokenAddr.toLowerCase())) {
            const nativeBalance = await correctProvider.getBalance(userAddr);
            console.log(`[Wrap Check] Chain ${chainName} - Native: ${nativeBalance}, Wrapped: ${erc20Balance}`);

            // Trả về tổng: Sức mua = Native + Wrapped
            return erc20Balance + nativeBalance;
        }

        return erc20Balance;
    } catch (error) {
        console.warn(`[Raw Call Error] Unable to read token ${tokenAddr} on chain ${chainName}:`, error);
        return 0n;
    }
}

// Hàm bổ sung: Tự động kiểm tra số dư trong ví để hiển thị đối chiếu
async function checkWalletBalances(token0Addr, token1Addr, token0Decimals, token1Decimals) {
    if (!state.user) return; // Không cần check state.readProvider nữa vì hàm balance tự build provider

    console.log("Checking wallet balances...");
    console.log("Token 0 Address: ", token0Addr);
    console.log("Token 1 Address: ", token1Addr);

    try {
        // Đã xóa 2 dòng khởi tạo ethers.Contract thừa thãi không dùng tới

        // Gọi hàm getSafeMemeBalance đã tích hợp tính năng check Native
        const bal0Raw = await getSafeMemeBalance(token0Addr, state.user);
        const bal1Raw = await getSafeMemeBalance(token1Addr, state.user);

        state.bal0Raw = bal0Raw;
        state.bal1Raw = bal1Raw;

        console.log("Total Balance 0 (Raw): ", bal0Raw);
        console.log("Total Balance 1 (Raw): ", bal1Raw);

        UI.balT0.innerText = parseFloat(ethers.formatUnits(bal0Raw, token0Decimals)).toFixed(4);
        UI.balT1.innerText = parseFloat(ethers.formatUnits(bal1Raw, token1Decimals)).toFixed(4);

        if (UI.btnMaxT0) UI.btnMaxT0.classList.remove('hidden');
        if (UI.btnMaxT1) UI.btnMaxT1.classList.remove('hidden');
    } catch (err) {
        console.warn("Fetch balance error", err);
    }
}

function renderPipelineUI(data) {
    const steps = [];
    steps.push({ id: 'wrap', name: 'Wrap Native', desc: 'Transfer Native to Wrapped' });
    const swapPlan = data.swap_step || data.swap_intent;
    if (swapPlan) steps.push({ id: 'swap', name: 'Zap Swap (Balance)', desc: swapPlan.description || 'Route pending' });
    steps.push({ id: 'approve', name: 'Approve Tokens', desc: 'Approve Token for NPM' });
    steps.push({ id: 'mint', name: 'Mint NFT & Auto-Stake', desc: 'Mint NFT Position' });
    if (data.metadata.pid || data.metadata.stake_method === 'aerodrome_gauge_deposit') {
        steps.push({ id: 'stake', name: 'Stake into Farm', desc: 'Stake NFT to earn rewards' });
    }

    UI.pipelineContainer.innerHTML = steps.map((s, i) => `
        <div class="pipeline-step" id="step_${s.id}">
            <div class="step-number">${i + 1}</div>
            <span class="text-xs font-bold flex-1">${s.name}</span>
            <div class="status-indicator" id="ind_${s.id}"></div>
        </div>
    `).join('');
}

// ============================================
// 4. THỰC THI PIPELINE (CLIENT-SIDE BUILD)
// ============================================

async function runExecutionFlow() {
    if (state.isProcessing || !state.currentPlan) return;
    state.isProcessing = true;
    setExecuteButtonState("executing");

    try {
        if (getCurrentSwapPlan()) {
            const routeReady = await ensureRoutePreviewForExecution();
            if (!routeReady) return;
        }

        const plan = state.currentPlan.strategy_analysis;
        const meta = state.currentPlan.metadata;
        const isAerodrome = meta.dex_type === 'aerodrome_v3' || meta.mint_param_schema === 'aerodrome_tick_spacing';
        state.dexType = meta.dex_type || state.dexType;
        const npmAddr = meta.npm_address || NPM_ADDRESSES[state.chain];
        if (!npmAddr) {
            throw new Error(`Missing NPM address config for chain ${state.chain}`);
        }
        console.log("Metadata and Plan for execution:", meta, plan);
        renderPipelineUI(state.currentPlan);

        // 1. WRAP (PHẢI LÀM TRƯỚC TIÊN)
        await executeStep('wrap', async () => {
            let totalNeeded0 = BigInt(plan.amount0_raw);
            let totalNeeded1 = BigInt(plan.amount1_raw);

            const swapPlan = getCurrentSwapPlan();
            if (swapPlan) {
                // CHỐNG LỖI 2: Đảm bảo backend đã update code mới nhất, trả về sell_amount_raw.
                if (!swapPlan.sell_amount_raw) {
                    throw new Error("Swap data from Backend is missing. Please ensure you have the latest version of the Backend code!");
                }

                const tokenInAddr = swapPlan.token_in_address || meta.token0_address;
                const swapAmt = BigInt(swapPlan.sell_amount_raw);

                if (tokenInAddr.toLowerCase() === meta.token0_address.toLowerCase()) {
                    totalNeeded0 += swapAmt;
                } else if (tokenInAddr.toLowerCase() === meta.token1_address.toLowerCase()) {
                    totalNeeded1 += swapAmt;
                }
            }

            await ensureWrap(meta.token0_address, totalNeeded0);
            await ensureWrap(meta.token1_address, totalNeeded1);
        });

        // 2. SWAP
        if (getCurrentSwapPlan()) {
            await executeStep('swap', async () => {
                let swapStep = await fetchFullSwapStepForExecution();
                guardRouteChangeForExecution(swapStep);
                let swapTx = swapStep.tx;
                let tokenInAddr = swapStep.token_in_address || meta.token0_address;
                let sellAmt = BigInt(swapStep.sell_amount_raw);

                // 1. Dùng readProvider để kiểm tra xem Token có tồn tại trên mạng này không
                const code = await state.readProvider.getCode(tokenInAddr);
                if (code === "0x") {
                    throw new Error(`Token ${tokenInAddr} does not exist on chain ${state.chain}. You are connecting to the wrong chain on MetaMask!`);
                }

                if (tokenInAddr.toLowerCase() !== "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee") {
                    // 2. Dùng readProvider kiểm tra số dư và allowance
                    const readContract = new ethers.Contract(tokenInAddr, ERC20_ABI, state.readProvider);
                    const actualBal = await readContract.balanceOf(state.user);

                    if (actualBal < sellAmt) {
                        const decimals = tokenInAddr.toLowerCase() === meta.token0_address.toLowerCase() ? meta.token0_decimals : meta.token1_decimals;
                        throw new Error(`Insufficient balance to Swap! Transaction blocked to save Gas.`);
                    }

                    const spender = swapStep.allowanceTarget || swapTx.to;
                    if (!spender) {
                        throw new Error("Swap route is missing allowance target.");
                    }
                    const allowance = await readContract.allowance(state.user, spender);
                    console.log(`Allowance to swap: ${allowance} for ${sellAmt}`);

                    // 3. Nếu thiếu, dùng Signer để approve
                    if (allowance < sellAmt) {
                        console.log(`Approve for correct Target: ${spender}`);
                        const writeContract = new ethers.Contract(tokenInAddr, ERC20_ABI, state.signer);
                        const approveTx = await writeContract.approve(spender, ethers.MaxUint256);
                        await approveTx.wait();
                        swapStep = await fetchFullSwapStepForExecution();
                        guardRouteChangeForExecution(swapStep);
                        swapTx = swapStep.tx;
                        tokenInAddr = swapStep.token_in_address || meta.token0_address;
                        sellAmt = BigInt(swapStep.sell_amount_raw);
                        const refreshedSpender = swapStep.allowanceTarget || swapTx.to;
                        if (!refreshedSpender) {
                            throw new Error("Swap route is missing allowance target after re-quote.");
                        }
                        const refreshedAllowance = await readContract.allowance(state.user, refreshedSpender);
                        if (refreshedAllowance < sellAmt) {
                            console.log(`Approve refreshed Target: ${refreshedSpender}`);
                            const approveTx2 = await writeContract.approve(refreshedSpender, ethers.MaxUint256);
                            await approveTx2.wait();
                            swapStep = await fetchFullSwapStepForExecution();
                            guardRouteChangeForExecution(swapStep);
                            swapTx = swapStep.tx;
                            tokenInAddr = swapStep.token_in_address || meta.token0_address;
                            sellAmt = BigInt(swapStep.sell_amount_raw);
                        }
                    }
                }

                if (getQuoteAgeMs(swapStep) > SWAP_QUOTE_MAX_AGE_MS) {
                    swapStep = await fetchFullSwapStepForExecution();
                    guardRouteChangeForExecution(swapStep);
                    swapTx = swapStep.tx;
                    tokenInAddr = swapStep.token_in_address || meta.token0_address;
                    sellAmt = BigInt(swapStep.sell_amount_raw);
                }

                // --- CƠ CHẾ RETRY CHỐNG LAG RPC METAMASK ---
                let tx;
                let lastErr;
                for (let i = 0; i < 2; i++) {
                    if (tokenInAddr.toLowerCase() !== "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee") {
                        const spender = swapStep.allowanceTarget || swapTx.to;
                        if (!spender) {
                            throw new Error("Swap route is missing allowance target before send.");
                        }
                        const readContract = new ethers.Contract(tokenInAddr, ERC20_ABI, state.readProvider);
                        const allowance = await readContract.allowance(state.user, spender);
                        if (allowance < sellAmt) {
                            const writeContract = new ethers.Contract(tokenInAddr, ERC20_ABI, state.signer);
                            const approveTx = await writeContract.approve(spender, ethers.MaxUint256);
                            await approveTx.wait();
                            swapStep = await fetchFullSwapStepForExecution();
                            guardRouteChangeForExecution(swapStep);
                            swapTx = swapStep.tx;
                            tokenInAddr = swapStep.token_in_address || meta.token0_address;
                            sellAmt = BigInt(swapStep.sell_amount_raw);
                            continue;
                        }
                    }
                    if (getQuoteAgeMs(swapStep) > SWAP_QUOTE_MAX_AGE_MS) {
                        swapStep = await fetchFullSwapStepForExecution();
                        guardRouteChangeForExecution(swapStep);
                        swapTx = swapStep.tx;
                        tokenInAddr = swapStep.token_in_address || meta.token0_address;
                        sellAmt = BigInt(swapStep.sell_amount_raw);
                        continue;
                    }
                    try {
                        tx = await state.signer.sendTransaction({
                            to: swapTx.to,  // Tx vẫn gửi cho Exchange Proxy bình thường
                            data: swapTx.data,
                            value: swapTx.value
                        });
                        break;
                    } catch (err) {
                        lastErr = err;
                        if (err.message && (err.message.includes("transfer amount exceeds balance") || err.message.includes("execution reverted"))) {
                            console.warn(`[Retry ${i + 1}/2] Swap route failed before broadcast. Fetching a fresh route...`);
                            swapStep = await fetchFullSwapStepForExecution();
                            guardRouteChangeForExecution(swapStep);
                            swapTx = swapStep.tx;
                            tokenInAddr = swapStep.token_in_address || meta.token0_address;
                            sellAmt = BigInt(swapStep.sell_amount_raw);
                        } else {
                            console.log(err.message);
                            throw err;
                        }
                    }
                }

                if (!tx) {
                    if (lastErr && lastErr.message && lastErr.message.includes("transfer amount exceeds balance")) {
                        throw new Error("MetaMask rejected the swap because the route became invalid or the balance changed. Refresh the route and try again.");
                    }
                    throw lastErr;
                }

                await tx.wait();
            });
        }

        // 3. APPROVE
        await executeStep('approve', async () => {
            await checkAndApprove(meta.token0_address, npmAddr, plan.amount0_raw);
            await checkAndApprove(meta.token1_address, npmAddr, plan.amount1_raw);
        });

        // 4. MINT POSITION
        let tokenId;
        await executeStep('mint', async () => {
            const npmAbi = isAerodrome ? AERODROME_NPM_ABI : PANCAKE_NPM_ABI;
            const npm = new ethers.Contract(npmAddr, npmAbi, state.signer);
            const deadline = Math.floor(Date.now() / 1000) + 1200;
            const mintParams = isAerodrome ? {
                token0: meta.token0_address, token1: meta.token1_address,
                tickSpacing: meta.tick_spacing || meta.fee_tier, tickLower: plan.range[0], tickUpper: plan.range[1],
                amount0Desired: plan.amount0_raw, amount1Desired: plan.amount1_raw,
                amount0Min: 0, amount1Min: 0,
                recipient: state.user,
                deadline,
                sqrtPriceX96: 0
            } : {
                token0: meta.token0_address, token1: meta.token1_address,
                fee: meta.fee_tier, tickLower: plan.range[0], tickUpper: plan.range[1],
                amount0Desired: plan.amount0_raw, amount1Desired: plan.amount1_raw,
                amount0Min: 0, amount1Min: 0, // Lưu ý: Nên set slippage bảo vệ thay vì 0
                recipient: state.user,
                deadline
            };
            const tx = await npm.mint(mintParams);
            const receipt = await tx.wait();
            tokenId = extractTokenId(receipt, npmAddr);
        });

        // 5. STAKE
        if (tokenId && (meta.pid || (isAerodrome && meta.staking_address))) {
            await executeStep('stake', async () => {
                const npmAbi = isAerodrome ? AERODROME_NPM_ABI : PANCAKE_NPM_ABI;
                const npm = new ethers.Contract(npmAddr, npmAbi, state.signer);
                if (isAerodrome) {
                    const gaugeAddress = meta.staking_address || meta.gauge_address;
                    if (!gaugeAddress) throw new Error("Missing Aerodrome gauge address.");
                    const approved = await npm.getApproved(tokenId);
                    if (approved.toLowerCase() !== gaugeAddress.toLowerCase()) {
                        const approveTx = await npm.approve(gaugeAddress, tokenId);
                        await approveTx.wait();
                    }
                    const gauge = new ethers.Contract(gaugeAddress, AERODROME_GAUGE_ABI, state.signer);
                    const tx = await gauge.deposit(tokenId);
                    await tx.wait();
                    return;
                }
                const coder = new ethers.AbiCoder();
                const data = coder.encode(["uint256"], [meta.pid]);
                const tx = await npm.safeTransferFrom(state.user, meta.masterchef_address, tokenId, data);
                await tx.wait();
            });
        }

        setExecuteButtonState("completed");
        showToast("🎉 Pipeline completed successfully!", "success");

    } catch (e) {
        console.error("Pipeline Error Detail:", e);

        // --- XỬ LÝ LỖI CHI TIẾT ĐỂ SHOW TOAST ---
        let errorMsg = "Transaction failed";

        if (e.isRouteReviewRequired) {
            showToast("Route changed. Review and click Execute again.", "warning");
            setExecuteButtonState("review");
            return;
        }

        // 1. Kiểm tra nếu user REJECT trên MetaMask (Lỗi phổ biến nhất)
        if (e.code === "ACTION_REJECTED" || e.code === 4001) {
            errorMsg = "You rejected to sign the transaction.";
            showToast(errorMsg, "warning");
        }
        // 2. Kiểm tra lỗi thiếu Gas (Native token)
        else if (e.message.includes("insufficient funds") || e.code === "INSUFFICIENT_FUNDS") {
            errorMsg = "Your wallet doesn't have enough native token to pay for the transaction.";
            showToast(errorMsg, "error");
        }
        // 3. Kiểm tra lỗi Swap (Thường do Slippage)
        else if (e.message.includes("execution reverted") || e.message.includes("transfer amount exceeds balance")) {
            errorMsg = "Transaction reverted. It may be due to a price slippage or a sudden change in balance.";
            showToast(errorMsg, "error");
        }
        // 4. Các lỗi khác
        else {
            errorMsg = e.reason || e.message || "An unknown error occurred.";
            showToast(`Error: ${errorMsg.slice(0, 50)}...`, "error");
        }

        // Reset trạng thái nút bấm để user có thể thử lại
        setExecuteButtonState("error", "RESTART PIPELINE");
    } finally {
        state.isProcessing = false;
    }
}

// --- Helpers cho Pipeline ---
async function executeStep(id, fn) {
    const el = document.getElementById(`step_${id}`);
    const ind = document.getElementById(`ind_${id}`);
    if (!el || !ind) {
        await fn();
        return;
    }

    el.classList.add('active');
    el.classList.remove('completed', 'error');
    ind.innerHTML = '<span class="loading loading-spinner loading-xs text-primary"></span>';
    try {
        await fn();
        ind.innerHTML = '✅';
        el.classList.remove('active');
        el.classList.add('completed');
    } catch (e) {
        ind.innerHTML = '❌';
        el.classList.remove('active');
        el.classList.add('error');
        console.log(e);
        throw e;
    }
}

async function ensureWrap(tokenAddr, neededRaw) {
    if (!WRAP_TOKENS.map(a => a.toLowerCase()).includes(tokenAddr.toLowerCase())) return;
    const contract = new ethers.Contract(tokenAddr, ERC20_ABI, state.signer);
    const bal = await contract.balanceOf(state.user);
    if (bal < BigInt(neededRaw)) {
        const tx = await contract.deposit({ value: BigInt(neededRaw) - bal });
        await tx.wait();
    }
}

// Tách việc đọc (Read) ra khỏi Signer
async function checkAndApprove(tokenAddr, spender, neededRaw) {
    // 1. Đọc allowance bằng readProvider
    const readContract = new ethers.Contract(tokenAddr, ERC20_ABI, state.readProvider);
    const allowance = await readContract.allowance(state.user, spender);

    // 2. Ký giao dịch bằng Signer (MetaMask) nếu thiếu
    if (allowance < BigInt(neededRaw)) {
        const writeContract = new ethers.Contract(tokenAddr, ERC20_ABI, state.signer);
        const tx = await writeContract.approve(spender, ethers.MaxUint256);
        await tx.wait();
    }
}

function extractTokenId(receipt, npmAddr) {
    const transferTopic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
    for (const log of receipt.logs) {
        if (log.address.toLowerCase() === npmAddr.toLowerCase() && log.topics[0] === transferTopic) {
            return ethers.getBigInt(log.topics[3]).toString();
        }
    }
    return null;
}

function showToast(msg, type = 'info') {
    // 1. Sinh ra một container hoàn toàn độc lập, không dùng chung với HTML cũ
    let container = document.getElementById('force-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'force-toast-container';
        // Ép style cứng bằng CSS thuần: Cố định góc trên bên phải, luôn nổi lên trên cùng
        container.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 2147483647; display: flex; flex-direction: column; gap: 10px; pointer-events: none;';
        document.body.appendChild(container);
    }

    // 2. Tạo nội dung Toast
    const toast = document.createElement('div');

    // Vẫn mượn class của DaisyUI cho đẹp, nhưng ép thêm style cứng
    toast.className = `alert alert-${type} shadow-lg`;
    toast.style.cssText = 'pointer-events: auto; min-width: 250px; opacity: 0; transform: translateX(50px); transition: all 0.4s cubic-bezier(0.68, -0.55, 0.265, 1.55);';

    let icon = "ℹ️";
    if (type === 'success') icon = "✅";
    if (type === 'error') icon = "❌";
    if (type === 'warning') icon = "⚠️";

    toast.innerHTML = `<span class="font-bold flex items-center gap-2">${icon} ${msg}</span>`;

    // 3. Đẩy vào màn hình
    container.appendChild(toast);

    // 4. Hiệu ứng bay vào mượt mà
    requestAnimationFrame(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateX(0)';
    });

    // 5. Tự động bay ra và xóa sau 4 giây
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(50px)';
        setTimeout(() => toast.remove(), 400); // Đợi animation chạy xong mới xóa DOM
    }, 4000);
}

let timer;
function debounce(f, t = 300) {
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => f.apply(this, args), t);
    };
}

function schedulePipelineRefresh(delay = 500) {
    if (state.isProcessing) return;
    resetQuotePreviewState(false);
    clearTimeout(pipelineRefreshTimer);
    pipelineRefreshTimer = setTimeout(() => refreshPipeline("preview"), delay);
}

init();
