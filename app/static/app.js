(function () {
  function groupBoxes(key) {
    return Array.prototype.slice.call(
      document.querySelectorAll('[data-check-item="' + key + '"]')
    );
  }

  function checkedInGroup(key) {
    return groupBoxes(key).filter(function (box) {
      return !box.disabled && box.checked;
    }).length;
  }

  function treeDescendantBoxes(box, key) {
    var node = box.closest("[data-location-node]");
    if (!node) return [];
    return Array.prototype.slice.call(
      node.querySelectorAll('[data-check-item="' + key + '"]')
    ).filter(function (child) { return child !== box; });
  }

  function syncTreeSelection(box, key) {
    if (!box.hasAttribute("data-check-tree")) return;
    var node = box.closest("[data-location-node]");
    if (!node) return;

    treeDescendantBoxes(box, key).forEach(function (child) {
      if (!child.disabled) child.checked = box.checked;
    });

    // 子库位取消勾选后，上级库位不能再随整棵树一起删除。
    if (!box.checked) {
      var parent = node.parentElement;
      while (parent) {
        if (parent.hasAttribute && parent.hasAttribute("data-location-node")) {
          var row = parent.firstElementChild;
          var parentBox = row ? row.querySelector('[data-check-item="' + key + '"]') : null;
          if (parentBox) parentBox.checked = false;
        }
        parent = parent.parentElement;
      }
    }
  }

  function updateTreeStates(key) {
    groupBoxes(key).forEach(function (box) {
      if (!box.hasAttribute("data-check-tree")) return;
      var descendants = treeDescendantBoxes(box, key).filter(function (child) {
        return !child.disabled;
      });
      if (!descendants.length) {
        box.indeterminate = false;
        return;
      }
      var selected = descendants.filter(function (child) { return child.checked; }).length;
      box.indeterminate = !box.checked && selected > 0 && selected < descendants.length;
    });
  }

  function updateGroup(key) {
    var boxes = groupBoxes(key);
    var usable = boxes.filter(function (box) { return !box.disabled; });
    var checked = usable.filter(function (box) { return box.checked; });
    document.querySelectorAll('[data-check-all="' + key + '"]').forEach(function (master) {
      master.disabled = usable.length === 0;
      master.checked = usable.length > 0 && checked.length === usable.length;
      master.indeterminate = checked.length > 0 && checked.length < usable.length;
    });
    document.querySelectorAll('[data-check-count="' + key + '"]').forEach(function (el) {
      el.textContent = "已选 " + checked.length + " 个";
    });
    updateTreeStates(key);
  }

  function passesRequired(form) {
    var key = form.getAttribute("data-require-checked");
    if (!key || checkedInGroup(key) > 0) return true;
    alert(form.getAttribute("data-check-message") || "请先勾选要操作的项目");
    return false;
  }

  // 删除确认（先检查是否已勾选）
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!passesRequired(f)) { e.preventDefault(); return; }
      if (!confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // 需要勾选的表单
  document.querySelectorAll("form[data-require-checked]:not([data-confirm])").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!passesRequired(f)) e.preventDefault();
    });
  });

  // 通用全选/计数
  var checkKeys = [];
  document.querySelectorAll("[data-check-all], [data-check-item], [data-check-count]").forEach(function (el) {
    var key = el.getAttribute("data-check-all") || el.getAttribute("data-check-item") || el.getAttribute("data-check-count");
    if (key && checkKeys.indexOf(key) < 0) checkKeys.push(key);
  });
  document.querySelectorAll("[data-check-all]").forEach(function (master) {
    var key = master.getAttribute("data-check-all");
    if (!key) return;
    master.addEventListener("change", function () {
      groupBoxes(key).forEach(function (box) {
        if (!box.disabled) box.checked = master.checked;
      });
      updateGroup(key);
    });
  });
  document.querySelectorAll("[data-check-item]").forEach(function (box) {
    var key = box.getAttribute("data-check-item");
    if (!key) return;
    box.addEventListener("change", function () {
      syncTreeSelection(box, key);
      updateGroup(key);
    });
  });
  checkKeys.forEach(updateGroup);

  // 旧版标签页全选
  var checkAll = document.getElementById("checkAll");
  if (checkAll) {
    checkAll.addEventListener("change", function () {
      document.querySelectorAll('input[name="ids"]').forEach(function (c) { c.checked = checkAll.checked; });
    });
  }

  // 标签按钮：未勾选时提示
  document.querySelectorAll("[data-label-submit]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      var n = document.querySelectorAll('input[name="ids"]:checked').length;
      if (!n) { e.preventDefault(); alert("请先勾选要打印的项目"); }
    });
  });

  // T50Pro USB 打印机状态
  var printerBadge = document.getElementById("printerStatusBadge");
  var printerText = document.getElementById("printerStatusText");
  var printerRefresh = document.getElementById("printerRefresh");
  function refreshPrinterStatus() {
    if (!printerBadge || !printerText) return;
    printerBadge.className = "badge";
    printerBadge.textContent = "检测中…";
    printerText.textContent = "正在检测主机 USB 打印机…";
    fetch("/labels/printer-status", {
      cache: "no-store",
      headers: { "Accept": "application/json" },
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (data && data.ok) {
          printerBadge.className = "badge ok";
          printerBadge.textContent = "就绪";
          printerText.textContent = "已检测到打印机，可直接发送标签。";
          printerText.title = data.devicePath || "";
        } else {
          printerBadge.className = "badge warn";
          printerBadge.textContent = "未就绪";
          printerText.textContent = (data && (data.error || data.errorMsg)) || "未检测到打印机";
          printerText.title = (data && data.devicePath) || "";
        }
      })
      .catch(function () {
        printerBadge.className = "badge danger";
        printerBadge.textContent = "检测失败";
        printerText.textContent = "无法连接本机打印服务，请刷新页面重试。";
      });
  }
  if (printerBadge) {
    refreshPrinterStatus();
    if (printerRefresh) printerRefresh.addEventListener("click", refreshPrinterStatus);
  }

  // ===== 标签页：多选下拉（支持 Shift 连选） =====
  function initLabelPicker(root) {
    var form = root.closest("form");
    var toggle = root.querySelector("[data-ms-toggle]");
    var panel = root.querySelector("[data-ms-panel]");
    if (!form || !toggle || !panel) return;

    var search = panel.querySelector("[data-ms-search]");
    var empty = panel.querySelector("[data-ms-empty]");
    var options = Array.prototype.slice.call(panel.querySelectorAll("[data-ms-option]"));
    var countEls = form.querySelectorAll("[data-ms-count]");
    var chipsEl = form.querySelector("[data-ms-chips]");
    var hintEl = form.querySelector("[data-ms-hint]");
    var toggleText = toggle.querySelector("[data-ms-toggle-text]");
    var idleText = toggleText ? toggleText.textContent : "选择";
    var lastIndex = -1;

    function close() {
      panel.hidden = true;
      root.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
      document.body.classList.remove("ms-open");
    }

    function openPanel() {
      panel.hidden = false;
      root.classList.add("open");
      toggle.setAttribute("aria-expanded", "true");
      document.body.classList.add("ms-open");
      if (search) {
        search.value = "";
        filterOptions("");
        setTimeout(function () { search.focus(); }, 30);
      }
    }

    function chipLabel(box) {
      var label = box.getAttribute("data-label") || box.value;
      var sub = box.getAttribute("data-sub") || "";
      return sub && sub !== label ? label + " · " + sub : label;
    }

    function refresh() {
      var checked = Array.prototype.slice.call(
        form.querySelectorAll('input[name="ids"]')
      ).filter(function (box) { return box.checked; });

      Array.prototype.forEach.call(countEls, function (el) {
        el.textContent = checked.length;
      });
      if (toggleText) {
        toggleText.textContent = checked.length ? "已选 " + checked.length + " 项" : idleText;
      }
      options.forEach(function (input) {
        var row = input.closest(".ms-option");
        if (row) row.classList.toggle("on", input.checked);
      });

      if (chipsEl) {
        chipsEl.innerHTML = "";
        checked.forEach(function (box) {
          var chip = document.createElement("button");
          chip.type = "button";
          chip.className = "ms-chip";
          chip.title = "移除 " + chipLabel(box);
          chip.innerHTML = '<span>' + esc(chipLabel(box)) + '</span>' +
            '<span class="ms-chip-x" aria-hidden="true">×</span>';
          chip.addEventListener("click", function () {
            box.checked = false;
            refresh();
          });
          chipsEl.appendChild(chip);
        });
        chipsEl.hidden = checked.length === 0;
      }
      if (hintEl) hintEl.hidden = checked.length > 0;
    }

    function filterOptions(query) {
      var q = String(query || "").trim().toLowerCase();
      var visible = 0;
      options.forEach(function (input) {
        var row = input.closest(".ms-option");
        if (!row) return;
        var hay = (row.getAttribute("data-search") || row.textContent || "").toLowerCase();
        var show = !q || hay.indexOf(q) !== -1;
        row.hidden = !show;
        if (show) visible += 1;
      });
      if (empty) empty.hidden = visible > 0;
    }

    options.forEach(function (input, index) {
      var row = input.closest(".ms-option");
      if (!row) return;
      // Shift 点击：以最近一次点击为起点，把中间的可见项一起选中/取消。
      row.addEventListener("click", function (e) {
        if (!e.shiftKey || lastIndex < 0) return;
        e.preventDefault();
        var state = !input.checked;
        var start = Math.min(lastIndex, index);
        var end = Math.max(lastIndex, index);
        for (var i = start; i <= end; i += 1) {
          var box = options[i];
          var target = box.closest(".ms-option");
          if (target && target.hidden) continue;
          box.checked = state;
        }
        lastIndex = index;
        refresh();
      });
      input.addEventListener("change", function () {
        lastIndex = index;
        refresh();
      });
    });

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      if (panel.hidden) openPanel();
      else close();
    });

    if (search) {
      search.addEventListener("input", function () { filterOptions(search.value); });
    }
    panel.querySelectorAll("[data-ms-all]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        options.forEach(function (input) {
          var row = input.closest(".ms-option");
          if (row && !row.hidden) input.checked = true;
        });
        refresh();
      });
    });
    panel.querySelectorAll("[data-ms-clear]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        Array.prototype.forEach.call(
          form.querySelectorAll('input[name="ids"]'),
          function (box) { box.checked = false; }
        );
        lastIndex = -1;
        refresh();
      });
    });
    panel.querySelectorAll("[data-ms-done]").forEach(function (btn) {
      btn.addEventListener("click", close);
    });

    document.addEventListener("click", function (e) {
      if (!root.contains(e.target)) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });

    refresh();
  }

  document.querySelectorAll("[data-ms]").forEach(initLabelPicker);

  // ===== 打印 / 导出下拉菜单 =====
  document.querySelectorAll("[data-menu]").forEach(function (menu) {
    var toggle = menu.querySelector("[data-menu-toggle]");
    var panel = menu.querySelector("[data-menu-panel]");
    if (!toggle || !panel) return;

    function closeMenu() {
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      menu.classList.remove("open");
    }

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var willOpen = panel.hidden;
      if (willOpen) {
        var rect = toggle.getBoundingClientRect();
        var below = window.innerHeight - rect.bottom;
        menu.classList.toggle("up", below < 240 && rect.top > below);
      }
      panel.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
      menu.classList.toggle("open", willOpen);
    });
    panel.addEventListener("click", function (e) {
      if (e.target.closest("[data-label-submit]")) closeMenu();
    });
    document.addEventListener("click", function (e) {
      if (!menu.contains(e.target)) closeMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeMenu();
    });
  });

  // ===== 移动端侧栏 =====
  var sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  function setSidebar(open) {
    document.body.classList.toggle("nav-open", open);
    if (sidebarToggle) sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", function () {
      setSidebar(!document.body.classList.contains("nav-open"));
    });
    document.querySelectorAll("[data-sidebar-close]").forEach(function (el) {
      el.addEventListener("click", function () { setSidebar(false); });
    });
    document.querySelectorAll("#appSidebar .nav a").forEach(function (a) {
      a.addEventListener("click", function () { setSidebar(false); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setSidebar(false);
    });
  }

  // ===== 拍照入库 =====
  var dz = document.getElementById("dropzone");
  if (!dz) return;

  var fileInput = document.getElementById("ocrFile");
  var previewImg = document.getElementById("previewImg");
  var previewWrap = document.getElementById("previewWrap");
  var recognizeBtn = document.getElementById("recognizeBtn");
  var statusEl = document.getElementById("ocrStatus");
  var imageField = document.getElementById("imageField");
  var jobField = document.getElementById("jobField");
  var rawWrap = document.getElementById("rawWrap");
  var rawText = document.getElementById("rawText");
  var codeText = document.getElementById("codeText");
  var matchesEl = document.getElementById("matches");
  var qtyBreakdown = document.getElementById("qtyBreakdown");
  var evidenceBox = document.getElementById("ocrEvidence");
  var selectedWrap = document.getElementById("selectedPart");
  var partIdField = document.getElementById("part_id");
  var modeInputs = document.querySelectorAll('input[name="mode"]');
  var newFields = document.getElementById("newPartFields");
  var searchInput = document.getElementById("existingSearch");
  var searchResults = document.getElementById("existingResults");
  var currentFile = null;
  var cameraOpen = document.getElementById("cameraOpen");
  var cameraShot = document.getElementById("cameraShot");
  var cameraClose = document.getElementById("cameraClose");
  var cameraState = document.getElementById("cameraState");
  var cameraWrap = document.getElementById("cameraWrap");
  var cameraVideo = document.getElementById("cameraVideo");
  var phoneCamera = document.getElementById("phoneCamera");
  var phoneCameraBtn = document.getElementById("phoneCameraBtn");
  var cameraStream = null;

  function setMode(mode) {
    modeInputs.forEach(function (r) { r.checked = r.value === mode; });
    if (newFields) newFields.style.display = mode === "new" ? "" : "none";
  }

  dz.addEventListener("click", function () { fileInput.click(); });
  ["dragenter", "dragover"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("drag"); });
  });
  dz.addEventListener("drop", function (e) {
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      fileInput.files = e.dataTransfer.files;
      handleFile(e.dataTransfer.files[0]);
    }
  });
  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files[0]) handleFile(fileInput.files[0]);
  });

  function handleFile(file) {
    currentFile = file;
    var url = URL.createObjectURL(file);
    previewImg.src = url;
    previewWrap.style.display = "";
    statusEl.textContent = "";
    recognizeBtn.disabled = false;
  }

  recognizeBtn.addEventListener("click", runOcr);

  function openCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      cameraState.textContent = "当前地址不支持摄像头（需本机 127.0.0.1 或 HTTPS），可改用「手机拍照识别」";
      return;
    }
    cameraState.textContent = "正在打开摄像头…";
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (stream) {
        cameraStream = stream;
        cameraVideo.srcObject = stream;
        cameraWrap.style.display = "";
        cameraOpen.style.display = "none";
        cameraShot.style.display = "";
        cameraClose.style.display = "";
        cameraState.textContent = "摄像头已开启";
        return cameraVideo.play();
      })
      .catch(function () {
        cameraState.textContent = "摄像头打开失败：请允许权限；手机请用 https 或本机 127.0.0.1 访问";
      });
  }

  function stopCamera() {
    if (cameraStream) {
      cameraStream.getTracks().forEach(function (t) { t.stop(); });
      cameraStream = null;
    }
    cameraVideo.srcObject = null;
    cameraWrap.style.display = "none";
    cameraOpen.style.display = "";
    cameraShot.style.display = "none";
    cameraClose.style.display = "none";
    cameraState.textContent = "";
  }

  function shootPhoto() {
    if (!cameraVideo.videoWidth) {
      cameraState.textContent = "摄像头还没准备好，稍等一下再拍";
      return;
    }
    var canvas = document.createElement("canvas");
    canvas.width = cameraVideo.videoWidth;
    canvas.height = cameraVideo.videoHeight;
    canvas.getContext("2d").drawImage(cameraVideo, 0, 0);
    canvas.toBlob(function (blob) {
      if (!blob) { cameraState.textContent = "拍照失败"; return; }
      var file = new File([blob], "camera_" + Date.now() + ".jpg", { type: "image/jpeg" });
      handleFile(file);
      stopCamera();
      runOcr();
    }, "image/jpeg", 0.92);
  }

  if (cameraOpen) cameraOpen.addEventListener("click", openCamera);
  if (cameraShot) cameraShot.addEventListener("click", shootPhoto);
  if (cameraClose) cameraClose.addEventListener("click", stopCamera);
  if (phoneCameraBtn && phoneCamera) {
    phoneCameraBtn.addEventListener("click", function () { phoneCamera.click(); });
    phoneCamera.addEventListener("change", function () {
      var f = phoneCamera.files && phoneCamera.files[0];
      if (!f) return;
      stopCamera();
      handleFile(f);
      phoneCamera.value = "";
      runOcr();
    });
  }
  window.addEventListener("beforeunload", stopCamera);

  function runOcr() {
    if (!currentFile) { alert("请先选择截图、标签图片，或用摄像头拍一张"); return; }
    recognizeBtn.disabled = true;
    statusEl.textContent = "识别中，首次加载模型可能需要十几秒…";
    var fd = new FormData();
    fd.append("file", currentFile);
    fetch("/api/ocr", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); })
      .then(function (res) {
        recognizeBtn.disabled = false;
        if (!res.ok) { statusEl.textContent = (res.data && res.data.error) || "识别失败"; return; }
        var d = res.data;
        var f = d.fields || {};
        var c = d.confidence || {};
        statusEl.textContent = "识别完成，请核对字段后确认入库";
        imageField.value = d.image || "";
        jobField.value = d.job_id || "";
        fill("model", f.model, c.model); fill("name", f.name, c.name); fill("brand", f.brand, c.brand);
        fill("package", f.package, c.package); fill("qty", f.qty, c.qty); fill("price", f.price, c.price);
        fill("supplier_part_no", f.supplier_part_no, c.supplier_part_no); fill("lot_no", f.lot_no, c.lot_no);
        fill("date_code", f.date_code, c.date_code);
        renderQty(d.qty_detail || {});
        renderEvidence(d);
        rawText.textContent = d.raw_text || "(没有识别到文字)";
        rawWrap.style.display = "";
        if (d.codes && d.codes.length) {
          codeText.textContent = "二维码/条码：" + d.codes.join("  |  ");
        } else {
          codeText.textContent = "";
        }
        renderMatches(d.matches || []);
        var lcscQuery = String(f.supplier_part_no || "").trim();
        if (!/^C\d{4,}$/i.test(lcscQuery)) lcscQuery = f.model || f.name || "";
        if (window.LcscWidget && lcscQuery) {
          window.LcscWidget.setQuery(lcscQuery, {
            package: f.package || "",
            brand: f.brand || "",
            auto: true
          });
        }
      })
      .catch(function (e) {
        recognizeBtn.disabled = false;
        statusEl.textContent = "请求失败：" + e;
      });
  }

  function fill(name, value, confidence) {
    var el = document.querySelector('[name="' + name + '"]');
    if (el && value) el.value = value;
    if (el) el.classList.toggle("field-low", typeof confidence === "number" && confidence > 0 && confidence < 0.7);
  }

  function renderQty(q) {
    if (!qtyBreakdown) return;
    if (!q || !q.formula) {
      qtyBreakdown.textContent = "";
      return;
    }
    qtyBreakdown.innerHTML = '<span class="badge blue">数量计算</span> ' + esc(q.formula) +
      (q.ambiguous ? ' <span class="badge warn">有多个规格数量，请核对</span>' : '');
  }

  function renderEvidence(d) {
    if (!evidenceBox) return;
    var f = d.fields || {};
    var conf = d.confidence || {};
    var ev = d.evidence || {};
    var sourceNames = { purchase: "购买页/订单截图", label: "商家标签", unknown: "未判断来源" };
    var labels = [
      ["model", "型号"], ["package", "封装"], ["qty", "数量"], ["price", "单价"],
      ["supplier_part_no", "料号"], ["brand", "品牌"], ["name", "名称"]
    ];
    var html = '<div class="evidence-head"><b>识别依据</b> <span class="badge gray">' +
      esc(sourceNames[d.source] || d.source || "未判断来源") + '</span></div>';
    if (d.qty_detail && d.qty_detail.formula) {
      html += '<div class="evidence-row"><span>数量规则</span><span></span><span class="small muted">' +
        esc(d.qty_detail.formula) + '</span></div>';
    }
    labels.forEach(function (pair) {
      var key = pair[0], label = pair[1];
      if (!f[key]) return;
      var pct = Math.round(Number(conf[key] || 0) * 100);
      var cls = pct >= 80 ? "ok" : (pct >= 60 ? "warn" : "danger");
      var text = (ev[key] && (ev[key].text || "")) || "";
      if (key === "qty" && d.qty_detail) text = d.qty_detail.formula || text;
      html += '<div class="evidence-row"><span>' + label + '</span>' +
        '<span class="badge ' + cls + '">' + pct + '%</span>' +
        '<span class="small muted">' + esc(String(text).slice(0, 80)) + '</span></div>';
    });
    html += '<div class="small muted" style="margin-top:6px">橙色输入框 = 低置信度，请重点核对。数量按“规格数量 × 购买数量”计算。</div>';
    evidenceBox.innerHTML = html;
    evidenceBox.style.display = "";
  }

  function renderMatches(list) {
    matchesEl.innerHTML = "";
    if (!list.length) {
      matchesEl.innerHTML = '<p class="muted small">没有找到相似物料，确认后会新建。</p>';
      return;
    }
    list.forEach(function (p) {
      var div = document.createElement("div");
      div.className = "match";
      div.innerHTML = '<div class="grow"><div class="m">' + esc(p.model) + '</div>' +
        '<div class="small muted">' + esc(p.name || "") + ' · ' + esc(p.package || "") + ' · 库存 ' + p.qty + ' · ' + esc(p.loc || "未分配") +
        (p.match_reason ? ' · ' + esc(p.match_reason) : '') + '</div></div>' +
        '<span class="badge blue">选择</span>';
      div.addEventListener("click", function () { selectPart(p); });
      matchesEl.appendChild(div);
    });
  }

  function selectPart(p) {
    partIdField.value = p.id;
    setMode("existing");
    selectedWrap.style.display = "";
    selectedWrap.innerHTML = '<div class="grow"><div class="m"><b>' + esc(p.model) + '</b> <span class="badge">' + esc(p.code) + '</span></div>' +
      '<div class="small muted">' + esc(p.name || "") + ' · 库存 ' + p.qty + ' · ' + esc(p.loc || "未分配") + '</div></div>' +
      '<button type="button" class="btn sm" id="clearSelected">改成新建</button>';
    document.getElementById("clearSelected").addEventListener("click", function () {
      partIdField.value = "";
      selectedWrap.style.display = "none";
      setMode("new");
    });
    searchResults.innerHTML = "";
  }

  modeInputs.forEach(function (r) {
    r.addEventListener("change", function () {
      if (r.checked && r.value === "new") {
        partIdField.value = "";
        selectedWrap.style.display = "none";
      }
      setMode(r.value);
    });
  });

  var timer = null;
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      clearTimeout(timer);
      var q = searchInput.value.trim();
      if (!q) { searchResults.innerHTML = ""; return; }
      timer = setTimeout(function () {
        fetch("/api/parts/search?q=" + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (list) {
            searchResults.innerHTML = "";
            if (!list.length) { searchResults.innerHTML = '<p class="muted small">没有匹配，直接新建即可。</p>'; return; }
            list.forEach(function (p) {
              var div = document.createElement("div");
              div.className = "match";
              div.innerHTML = '<div class="grow"><div class="m">' + esc(p.model) + '</div>' +
                '<div class="small muted">' + esc(p.name || "") + ' · 库存 ' + p.qty + ' · ' + esc(p.loc || "") + '</div></div>';
              div.addEventListener("click", function () { selectPart(p); });
              searchResults.appendChild(div);
            });
          });
      }, 220);
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
