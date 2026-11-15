(function () {
  var root = document.querySelector("[data-lcsc-widget]");
  if (!root) return;

  var mode = root.getAttribute("data-lcsc-mode") || "fill";
  var formSelector = root.getAttribute("data-lcsc-form");
  var form = formSelector ? document.querySelector(formSelector) : root.closest("form");
  var applyForm = root.querySelector("[data-lcsc-apply-form]");
  var queryEl = root.querySelector("[data-lcsc-query]");
  var packageEl = root.querySelector("[data-lcsc-package]");
  var brandEl = root.querySelector("[data-lcsc-brand]");
  var instockEl = root.querySelector("[data-lcsc-instock]");
  var statusEl = root.querySelector("[data-lcsc-status]");
  var resultsEl = root.querySelector("[data-lcsc-results]");
  var selectedEl = root.querySelector("[data-lcsc-selected]");
  var codeEl = root.querySelector("[data-lcsc-code]") ||
    (form ? form.querySelector("[data-lcsc-code]") : null);
  var modeEl = root.querySelector("[data-lcsc-mode-input]") ||
    (form ? form.querySelector("[data-lcsc-mode-input]") : null);
  var current = [];
  var controller = null;
  var lastPayload = null;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function iconSvg(name) {
    return '<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#i-' + name + '"></use></svg>';
  }

  function setStatus(html, bad) {
    if (!statusEl) return;
    statusEl.innerHTML = html || "";
    statusEl.className = "small lcsc-status" + (bad ? " bad" : " muted");
  }

  function money(value) {
    if (value == null || value === "") return "—";
    var n = Number(value);
    if (!isFinite(n)) return esc(value);
    var s = n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    return "¥" + s;
  }

  function priceText(item) {
    var tiers = item.price_tiers || [];
    if (!tiers.length) return item.price_min == null ? "价格未知" : money(item.price_min) + " 起";
    var first = tiers[0];
    var text = money(first.price) + " 起（" + first.qty + " 起）";
    if (item.price_min != null && Number(item.price_min) < Number(first.price)) {
      text += " · 最低 " + money(item.price_min);
    }
    return text;
  }

  function stockText(item) {
    var n = Number(item.stock || 0);
    if (!n) return '<span class="badge warn">无库存</span>';
    return '<span class="badge ok">库存 ' + n.toLocaleString("zh-CN") + "</span>";
  }

  function paramText(item) {
    var attrs = item.attributes || {};
    var keys = Object.keys(attrs);
    if (!keys.length) return "";
    var parts = [];
    for (var i = 0; i < keys.length && parts.length < 5; i++) {
      var key = keys[i];
      var value = String(attrs[key] || "").trim();
      if (!value || value.length > 60) continue;
      parts.push(key + ":" + value);
    }
    return parts.join(" · ");
  }

  function render(data) {
    lastPayload = data;
    current = data.results || [];
    if (data.warning) {
      setStatus('<span class="badge warn">提示</span> ' + esc(data.warning), true);
    } else {
      setStatus(
        '<span class="badge blue">' + esc(data.source || "立创商城") + '</span> ' +
        '找到 ' + Number(data.count || 0) + ' 个候选' +
        (data.cached ? ' · 短时缓存' : '') +
        ' · ' + esc(data.fetched_at || '') +
        ' · <a href="' + esc(data.manual_url || "#") + '" target="_blank" rel="noopener">在立创打开 ' + iconSvg('external') + '</a>'
      );
    }

    resultsEl.innerHTML = "";
    var visible = current.filter(function (item) {
      if (!instockEl || !instockEl.checked) return true;
      return Number(item.stock || 0) > 0 || item.is_exact_code || item.is_exact_mpn;
    });
    if (!visible.length) {
      var empty = document.createElement("div");
      empty.className = "lcsc-empty";
      empty.innerHTML = '没有可显示的候选。可以取消「只看有库存」，或' +
        ' <a href="' + esc(data.manual_url || "#") + '" target="_blank" rel="noopener">去立创商城手动查询 ' + iconSvg('external') + '</a>';
      resultsEl.appendChild(empty);
      return;
    }
    visible.forEach(function (item) {
      resultsEl.appendChild(buildCard(item));
    });
  }

  function buildCard(item) {
    var card = document.createElement("div");
    card.className = "lcsc-card" + (item.recommended ? " recommended" : "");
    var badges = "";
    if (item.is_exact_code) badges += ' <span class="badge ok">C 码精确</span>';
    else if (item.is_exact_mpn) badges += ' <span class="badge ok">型号精确</span>';
    if (item.recommended) badges += ' <span class="badge blue">推荐</span>';
    if (item.match_reason) badges += ' <span class="badge gray">' + esc(item.match_reason) + '</span>';

    var warnings = (item.warnings || []).map(function (w) {
      return '<div class="lcsc-warning">' + esc(w) + "</div>";
    }).join("");

    card.innerHTML =
      '<div class="lcsc-card-main">' +
        '<div class="lcsc-title"><b>' + esc(item.name || item.mpn || item.code) + '</b>' +
          ' <span class="badge">' + esc(item.code) + '</span>' + badges + '</div>' +
        '<div class="lcsc-meta small muted">' +
          (item.mpn ? '型号 ' + esc(item.mpn) + ' · ' : '') +
          (item.brand ? esc(item.brand) + ' · ' : '') +
          (item.package ? '封装 ' + esc(item.package) + ' · ' : '') +
          (item.category ? esc(item.category) : '') +
        '</div>' +
        '<div class="lcsc-price small">' + esc(priceText(item)) + ' ' + stockText(item) + '</div>' +
        (paramText(item) ? '<div class="lcsc-params small muted">' + esc(paramText(item)) + '</div>' : '') +
        warnings +
      '</div>' +
      '<div class="lcsc-card-actions">' +
        '<a class="btn sm" href="' + esc(item.product_url || "#") + '" target="_blank" rel="noopener">' + iconSvg('external') + '打开</a>' +
        '<button type="button" class="btn sm" data-lcsc-apply="blank">只补空</button>' +
        '<button type="button" class="btn sm primary" data-lcsc-apply="overwrite">以立创为准</button>' +
      '</div>';

    card.querySelectorAll("[data-lcsc-apply]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyCandidate(item, btn.getAttribute("data-lcsc-apply") === "overwrite");
      });
    });
    return card;
  }

  function search() {
    var q = (queryEl.value || "").trim();
    if (!q) {
      setStatus("请先输入型号、名称、参数或 C 码。", true);
      queryEl.focus();
      return;
    }
    if (controller) controller.abort();
    controller = window.AbortController ? new AbortController() : null;
    setStatus('<span class="badge blue">查询中</span> 正在连接立创商城，请稍候…');
    resultsEl.innerHTML = "";
    if (selectedEl) {
      selectedEl.hidden = true;
      selectedEl.innerHTML = "";
    }
    if (codeEl) codeEl.value = "";
    if (modeEl) modeEl.value = "blank";
    var url = "/api/lcsc/search?q=" + encodeURIComponent(q) +
      "&package=" + encodeURIComponent((packageEl && packageEl.value) || "") +
      "&brand=" + encodeURIComponent((brandEl && brandEl.value) || "") +
      "&limit=30";
    var opts = controller ? { signal: controller.signal } : {};
    fetch(url, opts)
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      })
      .then(function (res) {
        if (!res.ok) {
          var msg = (res.data && res.data.error) || "立创查询失败";
          var manual = (res.data && res.data.manual_url) || ("https://so.szlcsc.com/global.html?k=" + encodeURIComponent(q));
          setStatus('<span class="badge danger">失败</span> ' + esc(msg) +
            ' <a href="' + esc(manual) + '" target="_blank" rel="noopener">打开立创商城手动查询 ' + iconSvg('external') + '</a>', true);
          return;
        }
        render(res.data);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;
        setStatus('<span class="badge danger">失败</span> 网络请求失败：' + esc(err), true);
      });
  }

  function fillField(name, value, overwrite) {
    if (!form) return;
    var el = form.querySelector('[name="' + name + '"]');
    if (!el || value == null || value === "") return;
    if (!overwrite && String(el.value || "").trim()) return;
    el.value = value;
  }

  function currentSupplierIsLcscOrEmpty() {
    if (!form) return true;
    var select = form.querySelector('[name="supplier_id"]');
    if (!select) return true;
    if (!select.value) return true;
    var opt = select.options[select.selectedIndex];
    var text = opt ? (opt.text || "") : "";
    return /立创|lcsc/i.test(text);
  }

  function fillSupplierPartNo(code, overwrite) {
    if (!form) return;
    var el = form.querySelector('[name="supplier_part_no"]');
    if (!el) return;
    // 主供应商是淘宝/其他时，C 码只写入「立创来源」，不覆盖主供应商料号。
    if (!currentSupplierIsLcscOrEmpty()) return;
    if (!overwrite && String(el.value || "").trim()) return;
    el.value = code;
  }

  function selectLcscSupplierIfEmpty(overwrite) {
    if (!form) return;
    var select = form.querySelector('[name="supplier_id"]');
    if (!select || select.value) return;
    for (var i = 0; i < select.options.length; i++) {
      if (/立创|lcsc/i.test(select.options[i].text || "")) {
        select.value = select.options[i].value;
        return;
      }
    }
  }

  function applyCandidate(item, overwrite) {
    if (mode === "server") {
      if (overwrite && !confirm("以立创为准会覆盖型号、名称、品牌、封装、链接和参数；不会改动库存、库位、批次和实际入库单价。继续？")) return;
      if (codeEl) codeEl.value = item.code || "";
      if (modeEl) modeEl.value = overwrite ? "overwrite" : "blank";
      if (!applyForm) {
        setStatus("页面没有应用表单，无法提交。", true);
        return;
      }
      applyForm.submit();
      return;
    }

    if (!form) {
      setStatus("没有找到要填入的表单。", true);
      return;
    }
    if (overwrite && !confirm("以立创为准会覆盖型号、名称、品牌、封装、链接和参数；不会改动入库数量、库位、批次和实际单价。继续？")) return;

    var mpn = item.mpn || "";
    if (mpn) fillField("model", mpn, overwrite);
    else if (!form.querySelector('[name="model"]') || !String(form.querySelector('[name="model"]').value || "").trim()) {
      fillField("model", item.name, overwrite);
    }
    fillField("name", item.name, overwrite);
    fillField("brand", item.brand, overwrite);
    fillField("package", item.package, overwrite);
    fillField("purchase_url", item.product_url, overwrite);
    fillField("datasheet_url", item.datasheet_url, overwrite);
    fillField("attributes", item.attributes_text, overwrite);
    fillSupplierPartNo(item.code, overwrite);
    selectLcscSupplierIfEmpty(overwrite);
    // 明确不填 price / qty / location_id / lot_no / date_code：
    // 这些是实际采购和入库数据，必须由用户自己确认。
    if (codeEl) codeEl.value = item.code || "";
    if (modeEl) modeEl.value = overwrite ? "overwrite" : "blank";
    if (selectedEl) {
      selectedEl.hidden = false;
      selectedEl.innerHTML =
        '<div class="grow"><div><b>已选择 ' + esc(item.code) + '</b> ' +
        '<span class="badge ' + (overwrite ? "warn" : "blue") + '">' +
        (overwrite ? "以立创为准" : "只补空") + '</span></div>' +
        '<div class="small muted">' + esc(item.mpn || item.name || "") +
        (item.package ? ' · ' + esc(item.package) : '') +
        '；未改动入库数量、库位、批次和实际单价。</div></div>';
    }
    setStatus('<span class="badge ok">已选择</span> ' + esc(item.code) +
      '。核对表单后点「确认入库」或「保存修改」。');
  }

  function setQuery(value, options) {
    options = options || {};
    queryEl.value = value || "";
    if (packageEl && options.package !== undefined) packageEl.value = options.package || "";
    if (brandEl && options.brand !== undefined) brandEl.value = options.brand || "";
    if (options.auto && (queryEl.value || "").trim()) search();
  }

  function clearAll() {
    queryEl.value = "";
    if (packageEl) packageEl.value = "";
    if (brandEl) brandEl.value = "";
    resultsEl.innerHTML = "";
    if (selectedEl) {
      selectedEl.hidden = true;
      selectedEl.innerHTML = "";
    }
    if (codeEl) codeEl.value = "";
    if (modeEl) modeEl.value = "blank";
    setStatus("搜索是实时的，不会把立创商品库保存到本机。");
  }

  root.querySelectorAll("[data-lcsc-search]").forEach(function (btn) {
    btn.addEventListener("click", search);
  });
  root.querySelectorAll("[data-lcsc-clear]").forEach(function (btn) {
    btn.addEventListener("click", clearAll);
  });
  if (queryEl) {
    queryEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        search();
      }
    });
  }
  if (instockEl) {
    instockEl.addEventListener("change", function () {
      if (lastPayload) render(lastPayload);
    });
  }

  window.LcscWidget = {
    setQuery: setQuery,
    search: search,
    clear: clearAll
  };
})();
