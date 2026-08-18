const clientId = "BEvq7i4__c3e5eQ7xuI2NuLUkmIpH1ArJK0ABln386Z46WkdpaE1inU1weMMCovlDolT8ppimLXW8_tAZUtC7e0";
const sessions = { evm: { isLogin: false, address: "" }, solana: { isLogin: false, address: "" } };
const providers = { evm: null, solana: null };
let currentChain = "";

// ========== Helper ==========
function truncatedAddress(text, startChars=6, endChars=4) {
  if (!text) return "";
  return text.length <= startChars + endChars ? text : text.slice(0, startChars) + "..." + text.slice(-endChars);
}

function saveSession(chain) {
  localStorage.setItem(`wallet_session_${chain}`, JSON.stringify(sessions[chain]));
}

function loadSessions() {
  ["evm","solana"].forEach(chain => {
    const data = localStorage.getItem(`wallet_session_${chain}`);
    if(data) sessions[chain] = JSON.parse(data);
  });
}

function updateUI() {
  const loginBtn = document.querySelector(".btn-login");
  const dropdown = document.querySelector(".wrapper-dropdown");

  const primarySession = sessions.evm.isLogin ? { type: "evm", ...sessions.evm }
                       : sessions.solana.isLogin ? { type: "solana", ...sessions.solana }
                       : null;

  if(primarySession) {
    loginBtn.style.display = "none";
    dropdown.style.display = "block";
    dropdown.querySelectorAll(".address-text").forEach(el => {
      el.textContent = truncatedAddress(primarySession.address);
    });

    const cfg = document.getElementById("wallet-logo-config").dataset;

    // Update logo
    const logoUrl = primarySession.type === "evm"
      ? cfg.metamask
      : cfg.phantom;

    dropdown.querySelectorAll(".wallet-logo").forEach(img => {
      img.src = logoUrl;
    });

  } else {
    loginBtn.style.display = "block";
    dropdown.style.display = "none";
    loginBtn.textContent = "Connect Wallet";
  }
}

// ========== Force Reset ==========
async function forceReset(chain=null) {
  try {
    if(!chain || chain==="solana") providers.solana?.disconnect && await providers.solana.disconnect();
    if(!chain || chain==="evm") providers.evm=null; // MetaMask EVM chỉ clear provider
  } catch(e){}

  if(!chain || chain==="solana") { providers.solana=null; sessions.solana={isLogin:false,address:""}; localStorage.removeItem('wallet_session_solana'); }
  if(!chain || chain==="evm") { providers.evm=null; sessions.evm={isLogin:false,address:""}; localStorage.removeItem('wallet_session_evm'); }

  currentChain="";
  updateUI();
}

// ========== UI ==========
function openChainSelector() {
  document.querySelector(".btn-login").style.display = "none";  
  document.querySelector(".chain-selector").style.display = "block";
}

function toggleDropdown() {
  document.querySelector(".dropdown-content").classList.toggle("active");
}

// ========== Connect ==========
async function connectWallet(chain) {
  document.querySelector(".chain-selector").style.display="none";

  // Reset other chain session để tránh ghi đè
  if(chain==="evm") await forceReset("solana");
  if(chain.includes("solana")) await forceReset("evm");

  currentChain = chain.includes("solana") ? "solana" : "evm";

  try {
    // --- EVM (MetaMask) ---
    if(chain==="evm") {
      let metamaskProvider = window.ethereum?.isMetaMask ? window.ethereum : null;
      if(window.ethereum?.providers?.length) metamaskProvider = window.ethereum.providers.find(p=>p.isMetaMask);
      if(!metamaskProvider) return alert("MetaMask not found");

      providers.evm = metamaskProvider;
      await metamaskProvider.request({
        method: "wallet_requestPermissions",
        params: [{ eth_accounts: {} }],
      });
      await checkConnection(providers.evm);
      saveSession("evm");
      return;
    }

    // --- Solana Phantom ---
    if(chain==="solana_phantom") {
      if(!window.solana?.isPhantom) return alert("Phantom not found");
      providers.solana = window.solana;
      await window.solana.connect({ onlyIfTrusted:false });
      await checkConnection(providers.solana);
      saveSession("solana");
      return;
    }

  } catch(err){ console.error("connectWallet error:",err); }
}

// ========== Disconnect ==========
async function disconnectWallet(chain=null) {
  await forceReset(chain);
  console.log("✅ Wallet disconnected");
}

// ========== Check & Verify ==========
async function checkConnection(provider) {
  try {
    if(!provider) { updateUI(); return; }

    if(currentChain==="evm") {
      const ethersProvider = new ethers.BrowserProvider(provider);
      const signer = await ethersProvider.getSigner();
      const walletAddress = await signer.getAddress();

      const { nonce } = await (await fetch("/api/get_nonce")).json();
      const signature = await signer.signMessage(nonce);

      const verify = await (await fetch("/api/verify_signature", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ address: walletAddress, signature, chain:"evm" })
      })).json();

      sessions.evm = { isLogin: verify.success, address: walletAddress };
      saveSession("evm");
    }

    if(currentChain==="solana") {
      const walletAddress = provider.publicKey?.toString();
      const { nonce } = await (await fetch("/api/get_nonce")).json();
      const encoded = new TextEncoder().encode(nonce);
      const signed = await provider.signMessage(encoded,"utf8");
      const signature = Array.from(signed.signature).map(b=>b.toString(16).padStart(2,"0")).join("");

      const verify = await (await fetch("/api/verify_signature", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ address: walletAddress, signature, chain:"solana" })
      })).json();

      sessions.solana = { isLogin: verify.success, address: walletAddress };
      saveSession("solana");
    }

    updateUI();
  } catch(err){ console.error("checkConnection error:",err); await forceReset(currentChain); }
}

// ========== Init ==========
window.addEventListener("load", async () => {
  loadSessions();

  // Nếu EVM đã login, set lại provider từ MetaMask
  if(sessions.evm.isLogin){
    let metamaskProvider = window.ethereum?.isMetaMask ? window.ethereum : null;
    if(window.ethereum?.providers?.length) metamaskProvider = window.ethereum.providers.find(p=>p.isMetaMask);
    providers.evm = metamaskProvider;
    currentChain = "evm";
  }

  // Nếu Solana đã login, kết nối lại Phantom (onlyIfTrusted)
  if(sessions.solana.isLogin && window.solana?.isPhantom){
    providers.solana = window.solana;
    try { await window.solana.connect({ onlyIfTrusted:true }); currentChain="solana"; } catch(e){ console.log("Phantom not auto-reconnect"); }
  }

  updateUI();
});




