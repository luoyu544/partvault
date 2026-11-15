(function () {
  // ===== 新建库位：单个 / 批量标签 =====
  var tabButtons = Array.prototype.slice.call(document.querySelectorAll("[data-location-tab]"));
  var tabPanels = Array.prototype.slice.call(document.querySelectorAll("[data-location-panel]"));

  function activateTab(name) {
    tabButtons.forEach(function (button) {
      var on = button.getAttribute("data-location-tab") === name;
      button.classList.toggle("on", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
    });
    tabPanels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-location-panel") !== name;
    });
  }

  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      activateTab(button.getAttribute("data-location-tab"));
    });
  });

  // ===== 库位树：折叠 / 快速定位 =====
  var tree = document.querySelector("[data-location-tree]");
  if (tree) {
    var treeToggles = Array.prototype.slice.call(tree.querySelectorAll("[data-tree-toggle]"));
    var jump = document.querySelector("[data-location-jump]");
    var storageKey = "partvault.locationTree.collapsed";
    var collapsedIds = {};

    try {
      var saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(saved)) {
        saved.forEach(function (id) { collapsedIds[String(id)] = true; });
      }
    } catch (e) {
      collapsedIds = {};
    }

    function saveCollapsed() {
      try {
        localStorage.setItem(storageKey, JSON.stringify(Object.keys(collapsedIds)));
      } catch (e) {
        // 浏览器禁用本地存储时，折叠状态只在当前页面有效。
      }
    }

    function directChildren(node) {
      for (var i = 0; i < node.children.length; i += 1) {
        if (node.children[i].hasAttribute("data-tree-children")) return node.children[i];
      }
      return null;
    }

    function directToggle(node) {
      var row = node.firstElementChild;
      return row ? row.querySelector("[data-tree-toggle]") : null;
    }

    function setCollapsed(node, isCollapsed, persist) {
      var children = directChildren(node);
      if (!children) return;
      var nodeId = node.getAttribute("data-location-node");
      var button = directToggle(node);
      var codeEl = node.querySelector(".tree-row .code");
      var label = codeEl ? codeEl.textContent.trim() : nodeId;
      children.hidden = isCollapsed;
      if (button) {
        button.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
        button.title = (isCollapsed ? "展开 " : "收起 ") + label + " 的子库位";
      }
      if (isCollapsed) {
        collapsedIds[nodeId] = true;
      } else {
        delete collapsedIds[nodeId];
      }
      if (persist !== false) saveCollapsed();
    }

    treeToggles.forEach(function (button) {
      var node = button.closest(".tree-node");
      if (!node) return;
      var nodeId = node.getAttribute("data-location-node");
      if (collapsedIds[nodeId]) setCollapsed(node, true, false);
      button.addEventListener("click", function () {
        var current = directChildren(node);
        if (current) setCollapsed(node, !current.hidden);
      });
    });

    document.querySelectorAll("[data-tree-expand]").forEach(function (button) {
      button.addEventListener("click", function () {
        treeToggles.forEach(function (toggle) {
          setCollapsed(toggle.closest(".tree-node"), false, false);
        });
        saveCollapsed();
      });
    });

    document.querySelectorAll("[data-tree-collapse]").forEach(function (button) {
      button.addEventListener("click", function () {
        treeToggles.forEach(function (toggle) {
          setCollapsed(toggle.closest(".tree-node"), true, false);
        });
        saveCollapsed();
      });
    });

    if (jump) {
      jump.addEventListener("change", function () {
        var targetId = jump.value;
        if (!targetId) return;
        var nodes = Array.prototype.slice.call(tree.querySelectorAll("[data-location-node]"));
        var target = null;
        nodes.forEach(function (node) {
          if (node.getAttribute("data-location-node") === targetId) target = node;
        });
        if (!target) return;

        // 展开目标库位的所有上级，再滚动并短暂高亮。
        var parent = target.parentElement;
        while (parent && parent !== tree) {
          if (parent.hasAttribute("data-tree-children")) {
            var parentNode = parent.parentElement;
            if (parentNode && parentNode.classList.contains("tree-node")) {
              setCollapsed(parentNode, false);
            }
          }
          parent = parent.parentElement;
        }

        tree.querySelectorAll(".tree-target").forEach(function (node) {
          node.classList.remove("tree-target");
        });
        target.classList.add("tree-target");
        if (target.scrollIntoView) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        window.setTimeout(function () {
          target.classList.remove("tree-target");
        }, 2200);
      });
    }
  }

  // ===== 批量新建：数量提示与连续编号生成 =====
  var area = document.getElementById("batchCodes");
  var countEl = document.querySelector("[data-batch-count]");
  if (!area) return;

  function entryCount() {
    return area.value.split(/\r?\n/).filter(function (line) {
      var value = line.trim();
      return value && value.charAt(0) !== "#";
    }).length;
  }

  function updateCount() {
    if (!countEl) return;
    var count = entryCount();
    countEl.textContent = count ? ("当前 " + count + " 个库位") : "每行一个，最多 200 个";
  }

  area.addEventListener("input", updateCount);
  updateCount();

  var generateBtn = document.querySelector("[data-generate-locations]");
  if (!generateBtn) return;

  generateBtn.addEventListener("click", function () {
    var prefix = (document.getElementById("genPrefix").value || "").trim();
    var start = parseInt(document.getElementById("genStart").value, 10);
    var end = parseInt(document.getElementById("genEnd").value, 10);
    var width = parseInt(document.getElementById("genWidth").value, 10);
    var nameTpl = (document.getElementById("genName").value || "").trim();

    if (!isFinite(start) || !isFinite(end) || !isFinite(width)) {
      alert("请填写有效的起始序号、结束序号和位数");
      return;
    }
    if (start > end) {
      alert("结束序号不能小于起始序号");
      return;
    }
    if (end - start + 1 > 200) {
      alert("一次最多生成 200 个库位");
      return;
    }

    width = Math.max(1, Math.min(6, width));
    var lines = [];
    for (var i = start; i <= end; i += 1) {
      var number = String(i);
      while (number.length < width) number = "0" + number;
      var code = prefix + number;
      var name = nameTpl
        .replace(/\{n\}/g, String(i))
        .replace(/\{code\}/g, code);
      lines.push(name ? code + "," + name : code);
    }

    area.value = lines.join("\n");
    updateCount();
    area.focus();
  });
})();
