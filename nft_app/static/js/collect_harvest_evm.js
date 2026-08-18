/**
 * Collect, Harvest, and Withdraw wallet actions for EVM V3 NFT positions.
 *
 * The backend builds unsigned transaction payloads. This module only connects
 * the wallet, verifies the connected account, sends transactions, and refreshes
 * the table row after a successful action.
 */

const COLLECT_CHAIN_IDS = {
    "BNB": 56,
    "ETH": 1,
    "BAS": 8453,
    "ARB": 42161,
    "LIN": 59144,
    "POL": 137
};

function getActionDex(button) {
    return (button.dataset.dex || "pancakeswap").toLowerCase();
}

function getActionNpmAddress(button) {
    return button.dataset.npmAddress || "";
}

function buildActionPayload(chain, nftId, userAddress, button) {
    return {
        chain: chain,
        nft_id: nftId,
        user_address: userAddress,
        dex: getActionDex(button),
        npm_address: getActionNpmAddress(button)
    };
}

function getTxSteps(txData) {
    if (Array.isArray(txData.steps) && txData.steps.length > 0) {
        return txData.steps;
    }
    return [{
        label: txData.action || "Transaction",
        to: txData.to,
        data: txData.data,
        value: txData.value || "0x0",
        sub_calls: txData.sub_calls || []
    }];
}

async function sendTxSteps(signer, txData, button, actionName, nftId) {
    const steps = getTxSteps(txData);
    let lastReceipt = null;

    for (let i = 0; i < steps.length; i += 1) {
        const step = steps[i];
        const stepLabel = step.label || `${actionName} step ${i + 1}`;
        button.textContent = steps.length > 1 ? `Sending ${i + 1}/${steps.length}` : "Sending...";
        console.log(`[${actionName}] Sending step ${i + 1}/${steps.length}: ${stepLabel} to ${step.to} for NFT #${nftId}`);

        const tx = await signer.sendTransaction({
            to: step.to,
            data: step.data,
            value: step.value || "0x0"
        });

        button.textContent = steps.length > 1 ? `Confirming ${i + 1}/${steps.length}` : "Confirming...";
        lastReceipt = await tx.wait();
        console.log(`[${actionName}] Step ${i + 1}/${steps.length} confirmed: ${lastReceipt.hash}`);
    }

    return lastReceipt;
}

async function ensureCollectWalletReady(chain) {
    if (!window.ethereum) {
        throw new Error("MetaMask is not installed. Please install MetaMask to use this feature.");
    }

    const provider = new ethers.BrowserProvider(window.ethereum, "any");
    await provider.send("eth_requestAccounts", []);

    const targetChainId = COLLECT_CHAIN_IDS[chain];
    if (targetChainId) {
        const network = await provider.getNetwork();
        if (Number(network.chainId) !== targetChainId) {
            try {
                await window.ethereum.request({
                    method: "wallet_switchEthereumChain",
                    params: [{ chainId: "0x" + targetChainId.toString(16) }]
                });
            } catch (switchError) {
                throw new Error(`Please switch MetaMask to ${chain} network manually.`);
            }
        }
    }

    const freshProvider = new ethers.BrowserProvider(window.ethereum, "any");
    const signer = await freshProvider.getSigner();
    const userAddress = await signer.getAddress();

    return { signer, userAddress };
}

function assertWalletMatches(userAddress, walletAddress) {
    if (userAddress.toLowerCase() !== walletAddress.toLowerCase()) {
        throw new Error(
            `Connected wallet (${userAddress.slice(0, 6)}...) does not match position owner ` +
            `(${walletAddress.slice(0, 6)}...). Please switch wallet.`
        );
    }
}

async function collectFee(chain, nftId, walletAddress, button) {
    const originalText = button.textContent;
    try {
        button.disabled = true;
        button.textContent = "Preparing...";

        const { signer, userAddress } = await ensureCollectWalletReady(chain);
        assertWalletMatches(userAddress, walletAddress);

        const res = await fetch("/api/v3/build-collect-tx", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildActionPayload(chain, nftId, userAddress, button))
        });
        const txData = await res.json();
        if (txData.error) throw new Error(txData.message || txData.error);

        await sendTxSteps(signer, txData, button, "Collect", nftId);
        button.textContent = "Done";

        await autoRefreshAfterAction(
            chain,
            walletAddress,
            nftId,
            getActionDex(button),
            getActionNpmAddress(button)
        );
    } catch (err) {
        console.error("[Collect] Error:", err);
        if (err.code === "ACTION_REJECTED" || err.code === 4001) {
            button.textContent = "Rejected";
        } else {
            button.textContent = "Error";
            alert(`Collect Fee Error: ${err.message || err}`);
        }
    } finally {
        setTimeout(() => {
            button.textContent = originalText;
            button.disabled = button.dataset.collectDisabled === "1";
        }, 3000);
    }
}

async function harvestReward(chain, nftId, walletAddress, button) {
    const originalText = button.textContent;
    try {
        button.disabled = true;
        button.textContent = "Preparing...";

        const { signer, userAddress } = await ensureCollectWalletReady(chain);
        assertWalletMatches(userAddress, walletAddress);

        const res = await fetch("/api/v3/build-harvest-tx", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildActionPayload(chain, nftId, userAddress, button))
        });
        const txData = await res.json();
        if (txData.error) throw new Error(txData.message || txData.error);

        await sendTxSteps(signer, txData, button, "Harvest", nftId);
        button.textContent = "Done";

        await autoRefreshAfterAction(
            chain,
            walletAddress,
            nftId,
            getActionDex(button),
            getActionNpmAddress(button)
        );
    } catch (err) {
        console.error("[Harvest] Error:", err);
        if (err.code === "ACTION_REJECTED" || err.code === 4001) {
            button.textContent = "Rejected";
        } else {
            button.textContent = "Error";
            alert(`Harvest Reward Error: ${err.message || err}`);
        }
    } finally {
        setTimeout(() => {
            button.textContent = originalText;
            button.disabled = false;
        }, 3000);
    }
}

async function withdrawPosition(chain, nftId, walletAddress, button) {
    const dex = getActionDex(button);
    const actionText = dex === "aerodrome"
        ? "If staked: Gauge withdraw/claim -> Remove liquidity(100%) -> Collect fees -> Burn NFT"
        : "Remove liquidity(100%) -> Collect fees -> Unstake NFT if staked";
    const confirmed = confirm(
        `WARNING: This will close or unstake position #${nftId}.\n\n` +
        `Actions: ${actionText}\n\n` +
        "This action CANNOT be undone. Continue?"
    );
    if (!confirmed) return;

    const originalText = button.textContent;
    try {
        button.disabled = true;
        button.textContent = "Preparing...";

        const { signer, userAddress } = await ensureCollectWalletReady(chain);
        assertWalletMatches(userAddress, walletAddress);

        const res = await fetch("/api/v3/build-withdraw-tx", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildActionPayload(chain, nftId, userAddress, button))
        });
        const txData = await res.json();
        if (txData.error) throw new Error(txData.message || txData.error);

        console.log(`[Withdraw] NFT #${nftId} | calls: ${txData.sub_calls}`);
        await sendTxSteps(signer, txData, button, "Withdraw", nftId);
        button.textContent = "Withdrawn";

        await autoRefreshAfterAction(
            chain,
            walletAddress,
            nftId,
            dex,
            getActionNpmAddress(button)
        );
    } catch (err) {
        console.error("[Withdraw] Error:", err);
        if (err.code === "ACTION_REJECTED" || err.code === 4001) {
            button.textContent = "Rejected";
        } else {
            button.textContent = "Error";
            alert(`Withdraw Error: ${err.message || err}`);
        }
    } finally {
        setTimeout(() => {
            button.textContent = originalText;
            button.disabled = false;
        }, 3000);
    }
}

async function autoRefreshAfterAction(chain, wallet, nftId, dex = "pancakeswap", npmAddress = "") {
    try {
        console.log(`[AutoRefresh] Refreshing position #${nftId}...`);

        const response = await fetch("/refresh", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                chain: chain,
                wallet_address: wallet,
                nft_id: nftId,
                dex: dex,
                npm_address: npmAddress
            })
        });

        const resData = await response.json();

        if (resData.status === "done" && resData.data) {
            const rowSelector = npmAddress
                ? `#nftTable tbody tr[data-nft-id='${nftId}'][data-npm-address='${npmAddress}']`
                : `#nftTable tbody tr[data-nft-id='${nftId}']`;
            const row = document.querySelector(rowSelector);
            if (row) {
                updatePositionRow(row, resData.data);
                console.log(`[AutoRefresh] Position #${nftId} refreshed on UI`);
            }
        } else {
            console.warn(`[AutoRefresh] Refresh returned unexpected data for #${nftId}`);
        }
    } catch (err) {
        console.error("[AutoRefresh] Error:", err);
    }
}

async function preflightAerodromeCollectButtons() {
    const buttons = document.querySelectorAll(".action-collect[data-dex='aerodrome']:not([disabled])");
    buttons.forEach(async (button) => {
        try {
            const res = await fetch("/api/v3/claimable-info", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    chain: button.dataset.chain,
                    nft_id: button.dataset.nft,
                    user_address: button.dataset.wallet,
                    dex: getActionDex(button),
                    npm_address: getActionNpmAddress(button)
                })
            });
            const info = await res.json();
            if (info.collect_disabled) {
                button.disabled = true;
                button.dataset.collectDisabled = "1";
                button.title = info.collect_disabled_reason || "Collect is disabled while this Aerodrome position is staked.";
            }
        } catch (err) {
            console.warn("[Collect preflight] Aerodrome check failed:", err);
        }
    });
}

function updatePositionRow(row, nftData) {
    const token0_symbol = String(nftData[3]);
    const token1_symbol = String(nftData[4]);

    const initial_date = formatDateCollect(nftData[9]);
    const current_date = formatDateCollect(nftData[32]);

    const type_dex = String(nftData[37]).toLowerCase();
    const reward_token = type_dex === "aerodrome" ? "AERO" : "CAKE";

    const safeSet = (selector, value) => {
        const el = row.querySelector(selector);
        if (el) el.textContent = value;
    };

    safeSet("[data-label='Date']", initial_date);
    safeSet("[data-token0-initial='token0-initial']", formatTokenAmountCollect(Number(nftData[10]), token0_symbol));
    safeSet("[data-token1-initial='token1-initial']", formatTokenAmountCollect(Number(nftData[11]), token1_symbol));
    safeSet("[data-token0-current='token0-current']", formatTokenAmountCollect(Number(nftData[13]), token0_symbol));
    safeSet("[data-token1-current='token1-current']", formatTokenAmountCollect(Number(nftData[14]), token1_symbol));
    safeSet("[data-token0-delta='token0-delta']", formatTokenAmountCollect((Number(nftData[13]) - Number(nftData[10])), token0_symbol));
    safeSet("[data-token1-delta='token1-delta']", formatTokenAmountCollect((Number(nftData[14]) - Number(nftData[11])), token1_symbol));
    safeSet("[data-token0-fee='token0-fee']", formatTokenAmountCollect(Number(nftData[18]), token0_symbol));
    safeSet("[data-token1-fee='token1-fee']", formatTokenAmountCollect(Number(nftData[19]), token1_symbol));

    safeSet("[data-label='Fee APR']", Number(nftData[21]).toFixed(2) + "%");
    safeSet("[data-label='Fee APR 1h']", Number(nftData[22]).toFixed(0) + "%");
    safeSet("[data-current-price='current-price']", Number(nftData[15]).toFixed(0));
    safeSet("[data-label='Delta $']", Number(nftData[16]).toFixed(0));
    safeSet("[data-label='Fee $']", Number(nftData[20]).toFixed(2));
    safeSet("[data-label='Pending CAKE']", Number(nftData[23]).toFixed(4) + " " + reward_token);
    safeSet("[data-label='CAKE Reward 1h']", Number(nftData[24]).toFixed(0) + "%");
    safeSet("[data-label='Farm APR 1h']", Number(nftData[26]).toFixed(0) + "%");
    safeSet("[data-label='Farm APR All']", Number(nftData[27]).toFixed(0) + "%");
    safeSet("[data-label='Time Created']", current_date);
    safeSet("[data-label='Status']", String(nftData[8]));

    row.style.transition = "background-color 0.3s";
    row.style.backgroundColor = "rgba(46, 204, 113, 0.15)";
    setTimeout(() => {
        row.style.backgroundColor = "";
    }, 2000);
}

function formatDateCollect(date) {
    const d = new Date(date);
    return d.getFullYear() + "-" +
        String(d.getMonth() + 1).padStart(2, "0") + "-" +
        String(d.getDate()).padStart(2, "0") + " " +
        String(d.getHours()).padStart(2, "0") + ":" +
        String(d.getMinutes()).padStart(2, "0") + ":" +
        String(d.getSeconds()).padStart(2, "0");
}

function formatTokenAmountCollect(value, tokenSymbol) {
    const token = tokenSymbol.toUpperCase();
    if (token.includes("ETH") || token.includes("BNB")) {
        return Number(value).toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 });
    } else if (token.includes("BTC")) {
        return Number(value).toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 });
    } else if (token.includes("SOL")) {
        return Number(value).toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 });
    }
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

document.addEventListener("click", (e) => {
    const btn = e.target.closest(".action-collect");
    if (btn) {
        collectFee(btn.dataset.chain, btn.dataset.nft, btn.dataset.wallet, btn);
        return;
    }

    const hBtn = e.target.closest(".action-harvest");
    if (hBtn) {
        harvestReward(hBtn.dataset.chain, hBtn.dataset.nft, hBtn.dataset.wallet, hBtn);
        return;
    }

    const wBtn = e.target.closest(".action-withdraw");
    if (wBtn) {
        withdrawPosition(wBtn.dataset.chain, wBtn.dataset.nft, wBtn.dataset.wallet, wBtn);
    }
});

// Do not run claimable-info preflight on page load.
// It performs multiple on-chain RPC reads per Aerodrome position and can make Home slow.
// The collect endpoint still validates staked Aerodrome positions when the user clicks Collect.
