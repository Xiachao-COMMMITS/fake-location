/* ============================================================
   iPhone 定位模拟控制器 - 前端逻辑
   Leaflet 地图 · 路径规划 · 速度控制 · 模拟调度 · 状态轮询
   ============================================================ */

const API = ""; // 同源，无需前缀

// ---------- 工具 ----------
const $ = (id) => document.getElementById(id);
const fmt = (n, d = 6) => (n == null ? "—" : Number(n).toFixed(d));
const fmtKm = (n) => (n == null ? "—" : Number(n).toFixed(3) + " km");

function toast(msg, type = "") {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast " + type;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function api(path, method = "GET", body = null) {
    const opt = { method, headers: {} };
    if (body) {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
    }
    let res;
    try {
        res = await fetch(API + path, opt);
    } catch (e) {
        toast("无法连接后端服务，请确认程序正在运行。" + e, "error");
        throw e;
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
    if (!res.ok) {
        const detail = (data && data.detail) || `请求失败 (${res.status})`;
        toast(detail, "error");
        throw new Error(detail);
    }
    return data;
}

// ---------- 地球距离 (与后端一致) ----------
const R_KM = 6371.0088;
function haversineKm(a, b) {
    const [lat1, lon1] = a, [lat2, lon2] = b;
    const toRad = (x) => (x * Math.PI) / 180;
    const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
    const s = Math.sin(dLat / 2) ** 2 +
        Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
    return 2 * R_KM * Math.asin(Math.sqrt(s));
}
function interp(a, b, f) {
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
}

// ============================================================
// 地图
// ============================================================
let map, streetLayer, satLayer, currentLayer = "street";
let pathLayer, ghostLayer, liveMarker = null, previewMarker = null;
const waypoints = []; // [ [lat,lon], ... ]
const markers = [];   // Leaflet marker 列表

function initMap() {
    map = L.map("map", { zoomControl: true, attributionControl: true }).setView([39.9087, 116.3975], 12);

    streetLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: "© OpenStreetMap",
    });
    satLayer = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19, attribution: "© Esri",
    });
    streetLayer.addTo(map);

    pathLayer = L.layerGroup().addTo(map);
    ghostLayer = L.layerGroup().addTo(map);

    map.on("click", (e) => addWaypoint([e.latlng.lat, e.latlng.lng]));

    $("layerBtn").onclick = () => {
        if (currentLayer === "street") {
            map.removeLayer(streetLayer); satLayer.addTo(map);
            currentLayer = "sat"; $("layerBtn").textContent = "🛰 卫星";
        } else {
            map.removeLayer(satLayer); streetLayer.addTo(map);
            currentLayer = "street"; $("layerBtn").textContent = "🗺 街道";
        }
    };
}

function wpIcon(idx, isStart, isEnd) {
    const color = isStart ? "#22c55e" : isEnd ? "#ef4444" : "#3b82f6";
    return L.divIcon({
        className: "wp-icon",
        html: `<div style="
            width:24px;height:24px;border-radius:50% 50% 50% 0;
            background:${color};transform:rotate(-45deg);
            border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.5);
            display:flex;align-items:center;justify-content:center;">
            <span style="transform:rotate(45deg);color:#fff;font-size:11px;font-weight:700;">${idx}</span></div>`,
        iconSize: [24, 24], iconAnchor: [12, 24],
    });
}

function addWaypoint(latLng, silent = false) {
    waypoints.push(latLng);
    const idx = waypoints.length;
    const isStart = idx === 1;
    const marker = L.marker(latLng, {
        draggable: true,
        icon: wpIcon(idx, isStart, false),
    }).addTo(map);

    marker.on("dragend", (e) => {
        const i = markers.indexOf(marker);
        if (i >= 0) {
            waypoints[i] = [e.target.getLatLng().lat, e.target.getLatLng().lng];
            redrawPath();
        }
    });
    marker.on("contextmenu", (e) => {
        // 右键删除
        const i = markers.indexOf(marker);
        if (i >= 0) {
            markers.splice(i, 1);
            waypoints.splice(i, 1);
            map.removeLayer(marker);
            renumberMarkers();
            redrawPath();
        }
    });
    markers.push(marker);
    redrawPath();
    if (!silent) toast(`已添加航点 ${idx}`, "success");
}

function renumberMarkers() {
    markers.forEach((m, i) => {
        m.setIcon(wpIcon(i + 1, i === 0, i === markers.length - 1 && markers.length > 1));
    });
}

function redrawPath() {
    pathLayer.clearLayers();
    ghostLayer.clearLayers();
    if (waypoints.length === 0) { updatePathInfo(); return; }

    if (waypoints.length >= 2) {
        L.polyline(waypoints, { color: "#3b82f6", weight: 4, opacity: 0.85 }).addTo(pathLayer);
        // 起终点特殊标记
        const last = waypoints.length - 1;
        if (markers.length) {
            markers[0].setIcon(wpIcon(1, true, false));
            markers[last].setIcon(wpIcon(last + 1, false, waypoints.length > 1));
        }
    }
    updatePathInfo();
}

function clearPath() {
    markers.forEach((m) => map.removeLayer(m));
    markers.length = 0;
    waypoints.length = 0;
    pathLayer.clearLayers();
    ghostLayer.clearLayers();
    redrawPath();
    toast("已清空路径", "warn");
}

function undoWaypoint() {
    if (!markers.length) return;
    const m = markers.pop();
    waypoints.pop();
    map.removeLayer(m);
    redrawPath();
}

function updatePathInfo() {
    $("wpCount").textContent = waypoints.length;
    let total = 0;
    for (let i = 1; i < waypoints.length; i++) total += haversineKm(waypoints[i - 1], waypoints[i]);
    $("totalKm").textContent = fmtKm(total);
    const speed = parseFloat($("speedInput").value) || 0;
    if (total > 0 && speed > 0) {
        const sec = (total / speed) * 3600;
        $("etaText").textContent = formatDuration(sec);
    } else {
        $("etaText").textContent = "—";
    }
}

function formatDuration(sec) {
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

// ============================================================
// 路径预览动画（不下发设备）
// ============================================================
let previewRAF = null;
function previewPath() {
    if (waypoints.length < 2) { toast("请至少添加 2 个航点", "warn"); return; }
    cancelAnimationFrame(previewRAF);
    ghostLayer.clearLayers();

    let total = 0;
    for (let i = 1; i < waypoints.length; i++) total += haversineKm(waypoints[i - 1], waypoints[i]);
    const speed = parseFloat($("speedInput").value) || 20;
    const durationMs = (total / speed) * 3600 * 1000; // 真实耗时可能很长，预览压缩
    const previewMs = Math.min(Math.max(durationMs, 2000), 15000); // 2~15 秒内播放

    L.polyline(waypoints, { color: "#22c55e", weight: 4, opacity: 0.5, dashArray: "8,8" }).addTo(ghostLayer);
    previewMarker = L.circleMarker(waypoints[0], {
        radius: 8, color: "#22c55e", fillColor: "#22c55e", fillOpacity: 1,
    }).addTo(ghostLayer);

    const start = performance.now();
    const step = (now) => {
        const t = Math.min((now - start) / previewMs, 1);
        const dist = total * t;
        // 找到位置
        let acc = 0, pos = waypoints[0];
        for (let i = 1; i < waypoints.length; i++) {
            const seg = haversineKm(waypoints[i - 1], waypoints[i]);
            if (acc + seg >= dist) {
                pos = interp(waypoints[i - 1], waypoints[i], (dist - acc) / seg);
                break;
            }
            acc += seg;
        }
        previewMarker.setLatLng(pos);
        if (t < 1) previewRAF = requestAnimationFrame(step);
        else { toast("预览完成", "success"); }
    };
    previewRAF = requestAnimationFrame(step);
}

// ============================================================
// 设备连接
// ============================================================
async function refreshDevices() {
    $("refreshBtn").disabled = true;
    try {
        const data = await api("/api/devices");
        const sel = $("deviceSelect");
        sel.innerHTML = "";
        if (!data.devices.length) {
            sel.innerHTML = '<option value="">— 未发现设备 —</option>';
            $("devModeBtn").disabled = true;
            $("devModeRevealBtn").disabled = true;
            setDevModeInfo("");
            toast("未发现设备，可点击「🔍 诊断」排查原因", "warn");
        } else {
            data.devices.forEach((d) => {
                const opt = document.createElement("option");
                opt.value = d.udid;
                opt.textContent = `${d.name} · iOS ${d.ios_version} (${d.udid.slice(0, 8)}…)`;
                sel.appendChild(opt);
            });
            // 默认选中第一台：启用开发者模式按钮并查询状态
            $("devModeBtn").disabled = false;
            $("devModeRevealBtn").disabled = false;
            checkDevModeStatus();
            toast(`发现 ${data.devices.length} 台设备`, "success");
        }
    } finally {
        $("refreshBtn").disabled = false;
    }
}

async function runDiagnostics() {
    const box = $("diagResult");
    const btn = $("diagBtn");
    btn.disabled = true;
    btn.textContent = "🔍 诊断中…";
    box.classList.remove("hidden");
    box.innerHTML = '<div class="diag-loading">正在逐层检测，请稍候…</div>';
    try {
        const d = await api("/api/diagnostics");
        const parts = [];
        parts.push(`<div class="diag-row ${d.pmd3_available ? "ok" : "err"}">
            <span class="diag-label">pymobiledevice3</span>
            <span>${d.pmd3_available ? "✓ 已安装" : "✗ 未安装"}</span>
        </div>`);
        parts.push(`<div class="diag-row ${d.usbmuxd_tcp ? "ok" : "err"}">
            <span class="diag-label">usbmuxd 服务 (127.0.0.1:27015)</span>
            <span>${d.usbmuxd_tcp ? "✓ 已连接" : "✗ 未监听"}</span>
        </div>`);
        const cliN = (d.cli_devices || []).length;
        parts.push(`<div class="diag-row ${cliN > 0 ? "ok" : "warn"}">
            <span class="diag-label">CLI 枚举设备数</span>
            <span>${cliN > 0 ? "✓ " + cliN + " 台" : "⚠ 0 台"}</span>
        </div>`);
        const asyncN = (d.async_devices || []).length;
        parts.push(`<div class="diag-row ${asyncN > 0 ? "ok" : "warn"}">
            <span class="diag-label">异步枚举设备数</span>
            <span>${asyncN > 0 ? "✓ " + asyncN + " 台" : "⚠ 0 台"}</span>
        </div>`);
        const pnpN = (d.pnp_apple_devices || []).length;
        if (pnpN > 0) {
            parts.push(`<div class="diag-row ok">
                <span class="diag-label">Windows PnP Apple 设备</span>
                <span>✓ ${pnpN} 个</span>
            </div>`);
            (d.pnp_apple_devices || []).forEach((p) => {
                parts.push(`<div class="diag-sub">${p.FriendlyName || "?"} — ${p.Status || "?"} (${p.Class || "?"})</div>`);
            });
        }
        if (d.recommendations && d.recommendations.length) {
            parts.push('<div class="diag-recs">');
            d.recommendations.forEach((r) => {
                parts.push(`<div class="diag-rec">${r}</div>`);
            });
            parts.push('</div>');
        }
        box.innerHTML = parts.join("");
    } catch (e) {
        box.innerHTML = `<div class="diag-row err">诊断失败: ${e?.message || e}</div>`;
    } finally {
        btn.disabled = false;
        btn.textContent = "🔍 诊断";
    }
}

async function connectDevice() {
    const udid = $("deviceSelect").value;
    if (!udid) { toast("请先选择设备", "warn"); return; }
    try {
        const data = await api("/api/connect", "POST", { udid });
        $("connBadge").textContent = `已连接: ${data.device.name}`;
        $("connBadge").className = "badge badge-ok";
        $("connectBtn").disabled = true;
        $("disconnectBtn").disabled = false;
        toast(`已连接 ${data.device.name}`, "success");
    } catch { /* toast 已由 api 处理 */ }
}

async function disconnectDevice() {
    const udid = $("deviceSelect").value;
    if (!udid) return;
    await api("/api/disconnect", "POST", { udid });
    $("connBadge").textContent = "未连接设备";
    $("connBadge").className = "badge badge-warn";
    $("connectBtn").disabled = false;
    $("disconnectBtn").disabled = true;
    resetSimUI();
    toast("已断开设备", "warn");
}

// ============================================================
// 开发者模式（iOS 16+，菜单默认隐藏时用此触发）
// ============================================================
function selectedUdid() { return $("deviceSelect").value; }

function setDevModeInfo(msg, type = "") {
    const box = $("devModeInfo");
    if (!msg) { box.classList.add("hidden"); box.textContent = ""; box.className = "devmode-info hidden"; return; }
    box.className = "devmode-info" + (type ? " " + type : "");
    box.textContent = msg;
}

async function triggerDevMode(revealOnly = false) {
    const udid = selectedUdid();
    if (!udid) { toast("请先选择设备", "warn"); return; }
    const btn = $("devModeBtn");
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = revealOnly ? "正在显示选项…" : "正在调出启用流程…";
    setDevModeInfo("正在向 iPhone 发送请求，请稍候（最多约 90 秒）…", "");
    try {
        const path = revealOnly ? "/api/developer-mode/reveal" : "/api/developer-mode/trigger";
        const data = await api(path, "POST", { udid });
        const msg = data.message || "请求已发送。";
        setDevModeInfo(
            msg + (revealOnly
                ? ""
                : "\n\n步骤：① 手机弹窗点「启用」→ 输入密码 → 确认重启；② 重启完成后回到这里点「连接」。"),
            "ok"
        );
        toast(revealOnly ? "已请求显示开发者模式选项" : "已发送启用请求，请在手机上确认", "success");
    } catch (e) {
        setDevModeInfo("请求失败：" + (e?.message || "未知错误") + "\n可改用「仅显示菜单」先让选项出现，再到手机设置里手动开启。", "err");
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function checkDevModeStatus() {
    const udid = selectedUdid();
    if (!udid) return;
    try {
        const s = await api("/api/developer-mode/status?udid=" + encodeURIComponent(udid));
        if (s.enabled === true) {
            setDevModeInfo("✓ 开发者模式已开启，可直接点「连接」。", "ok");
        } else if (s.enabled === false) {
            setDevModeInfo("开发者模式未开启。点「🔧 开启开发者模式」即可在手机上调出启用流程。", "");
        }
    } catch { /* 忽略，由连接流程兜底 */ }
}

// ============================================================
// 模拟控制
// ============================================================
function getPayload() {
    if (waypoints.length < 2) { toast("请至少添加 2 个航点", "warn"); return null; }
    const speed = parseFloat($("speedInput").value);
    if (!(speed > 0)) { toast("请设置有效速度", "warn"); return null; }
    let loop = parseInt($("loopInput").value, 10);
    if (isNaN(loop) || loop < 0) loop = 0;
    if (loop > 10000) loop = 10000;
    return {
        waypoints: waypoints.map(([lat, lon]) => ({ lat, lon })),
        speed_kmh: speed,
        loop_count: loop,
        jitter: $("jitterToggle").checked,
    };
}

async function startSim() {
    const payload = getPayload();
    if (!payload) return;
    try {
        await api("/api/simulate", "POST", payload);
        $("startBtn").disabled = true;
        $("pauseBtn").disabled = false;
        $("stopBtn").disabled = false;
        toast("模拟已开始", "success");
    } catch { }
}

async function pauseSim() {
    try {
        await api("/api/pause", "POST");
        $("pauseBtn").disabled = true;
        $("resumeBtn").disabled = false;
    } catch { }
}
async function resumeSim() {
    try {
        await api("/api/resume", "POST");
        $("resumeBtn").disabled = true;
        $("pauseBtn").disabled = false;
    } catch { }
}
async function stopSim() {
    try {
        await api("/api/stop", "POST");
        resetSimUI();
        toast("已停止模拟", "warn");
    } catch { }
}
async function restoreLocation() {
    if (!confirm("确认恢复 iPhone 的真实定位？这将清除模拟位置。")) return;
    try {
        const data = await api("/api/restore", "POST");
        resetSimUI();
        if (data.ok) {
            toast("已恢复真实定位", "success");
            if (liveMarker) { map.removeLayer(liveMarker); liveMarker = null; }
        } else {
            toast("恢复时出现问题: " + (data.detail || "未知"), "error");
        }
    } catch { }
}
async function exitApp() {
    if (!confirm("确认退出程序？将先恢复真实定位并终止所有模拟进程。")) return;
    try {
        await api("/api/exit", "POST");
        toast("程序正在退出…", "success");
        document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:#9aa7b4;font-family:sans-serif;">程序已退出，可关闭此窗口。</div>';
    } catch { }
}

function resetSimUI() {
    $("startBtn").disabled = false;
    $("pauseBtn").disabled = true;
    $("resumeBtn").disabled = true;
    $("stopBtn").disabled = true;
}

// ============================================================
// 状态轮询
// ============================================================
async function pollStatus() {
    try {
        const data = await api("/api/status");
        const sim = data.sim || {};
        const dev = data.connected_device;

        if (dev) {
            $("connBadge").textContent = `已连接: ${dev.name}`;
            $("connBadge").className = "badge badge-ok";
            $("connectBtn").disabled = true;
            $("disconnectBtn").disabled = false;
        } else {
            $("connBadge").textContent = "未连接设备";
            $("connBadge").className = "badge badge-warn";
        }

        const stateMap = { idle: "空闲", running: "运行中", paused: "已暂停", finished: "已完成", stopped: "已停止", error: "错误" };
        $("simState").textContent = stateMap[sim.state] || sim.state || "空闲";

        if (sim.current_lat != null && sim.current_lon != null) {
            $("curCoord").textContent = `${fmt(sim.current_lat)}, ${fmt(sim.current_lon)}`;
            // 在地图上显示实时位置
            const pos = [sim.current_lat, sim.current_lon];
            if (!liveMarker) {
                liveMarker = L.circleMarker(pos, {
                    radius: 9, color: "#fff", weight: 2,
                    fillColor: "#ef4444", fillOpacity: 1,
                }).addTo(map);
            } else {
                liveMarker.setLatLng(pos);
            }
        }

        if (sim.total_km > 0) {
            $("curProgress").textContent = `${fmt(sim.progress_km, 3)} / ${fmt(sim.total_km, 3)} km`;
            $("progressBar").style.width = `${Math.min(100, (sim.progress_km / sim.total_km) * 100)}%`;
        } else {
            $("curProgress").textContent = "—";
            $("progressBar").style.width = "0%";
        }
        $("curSpeed").textContent = sim.speed_kmh ? `${fmt(sim.speed_kmh, 1)} km/h` : "—";

        // 循环进度
        if (sim.total_loops && sim.total_loops > 1) {
            $("curLoop").textContent = `${sim.current_loop || 1} / ${sim.total_loops}`;
        } else {
            $("curLoop").textContent = "单次";
        }
        // 随机误差
        $("curJitter").textContent = sim.jitter ? "开 (±5m)" : "关";

        // 自动同步按钮状态
        if (sim.state === "running") {
            $("startBtn").disabled = true; $("pauseBtn").disabled = false;
            $("resumeBtn").disabled = true; $("stopBtn").disabled = false;
        } else if (sim.state === "paused") {
            $("startBtn").disabled = true; $("pauseBtn").disabled = true;
            $("resumeBtn").disabled = false; $("stopBtn").disabled = false;
        } else if (sim.state === "finished" || sim.state === "stopped" || sim.state === "error" || sim.state === "idle") {
            if (sim.state === "error" && sim.error) toast("模拟出错: " + sim.error, "error");
            $("startBtn").disabled = false; $("pauseBtn").disabled = true;
            $("resumeBtn").disabled = true; $("stopBtn").disabled = true;
            if (sim.state === "finished") toast("路径模拟已完成", "success");
        }
    } catch { /* 网络抖动忽略 */ }
}

// ============================================================
// 初始化
// ============================================================
async function init() {
    initMap();

    // 路径操作
    $("addCoordBtn").onclick = () => {
        const lat = parseFloat($("latInput").value);
        const lon = parseFloat($("lonInput").value);
        if (isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
            toast("请输入有效经纬度 (lat -90~90, lon -180~180)", "warn"); return;
        }
        addWaypoint([lat, lon]);
        map.setView([lat, lon], Math.max(map.getZoom(), 14));
        $("latInput").value = ""; $("lonInput").value = "";
    };
    $("undoBtn").onclick = undoWaypoint;
    $("clearBtn").onclick = clearPath;
    $("previewBtn").onclick = previewPath;

    // 速度
    const syncSpeed = (val) => {
        let v = parseFloat(val);
        if (isNaN(v) || v <= 0) v = 0.1;
        v = Math.min(Math.max(v, 0.1), 2000);
        $("speedRange").value = Math.min(v, 200);
        $("speedInput").value = v;
        updatePathInfo();
    };
    $("speedRange").oninput = (e) => syncSpeed(e.target.value);
    $("speedInput").oninput = (e) => syncSpeed(e.target.value);
    document.querySelectorAll(".chip").forEach((c) => {
        c.onclick = () => syncSpeed(c.dataset.speed);
    });

    // 设备
    $("refreshBtn").onclick = refreshDevices;
    $("diagBtn").onclick = runDiagnostics;
    $("connectBtn").onclick = connectDevice;
    $("disconnectBtn").onclick = disconnectDevice;
    $("devModeBtn").onclick = () => triggerDevMode(false);
    $("devModeRevealBtn").onclick = () => triggerDevMode(true);
    // 选中设备时启用开发者模式按钮并查询状态
    $("deviceSelect").onchange = () => {
        const has = !!$("deviceSelect").value;
        $("devModeBtn").disabled = !has;
        $("devModeRevealBtn").disabled = !has;
        if (has) checkDevModeStatus(); else setDevModeInfo("");
    };

    // 模拟控制
    $("startBtn").onclick = startSim;
    $("pauseBtn").onclick = pauseSim;
    $("resumeBtn").onclick = resumeSim;
    $("stopBtn").onclick = stopSim;
    $("restoreBtn").onclick = restoreLocation;
    $("exitBtn").onclick = exitApp;

    // 循环次数 / 随机误差
    $("loopInput").addEventListener("input", () => {
        let v = parseInt($("loopInput").value, 10);
        if (isNaN(v) || v < 0) v = 0;
        if (v > 10000) v = 10000;
        $("loopInput").value = v;
    });
    $("jitterToggle").onchange = (e) => {
        $("jitterHint").textContent = e.target.checked ? "已开启" : "关闭";
    };

    // 健康检查
    try {
        const h = await api("/api/health");
        if (h.dry_run) {
            $("envBadge").textContent = "模拟模式 (dry-run)";
            $("envBadge").className = "badge badge-warn";
            toast("当前为 dry-run 模拟模式，无需真机即可体验全部流程", "warn");
        } else if (!h.pmd3_available) {
            $("envBadge").textContent = "⚠ pymobiledevice3 未安装";
            $("envBadge").className = "badge badge-err";
            toast("pymobiledevice3 未安装，真机功能不可用。请见 README 安装依赖。", "error");
        } else {
            $("envBadge").textContent = "环境就绪";
            $("envBadge").className = "badge badge-ok";
        }
    } catch { }

    // 启动状态轮询
    pollStatus();
    setInterval(pollStatus, 1000);

    // 页面加载后自动刷新设备列表（无需用户手动点刷新）
    refreshDevices();
}

window.addEventListener("DOMContentLoaded", init);
