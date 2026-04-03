/**
 * accounts.js — User Accounts tab
 *
 * Manage Vista 20P user codes (01–49):
 *   User 01: Installer — password change only (via *20 programming field)
 *   User 02: Master    — password change only (code entered twice)
 *   Users 03–49: Add/delete code, set authority level, set partition
 *
 * There is no way to READ which users exist — this is write-only.
 * The panel beeps once to confirm a successful user code add/change.
 */

"use strict";

$(function () {

  const AUTHORITY_LEVELS = [
    { value: 0, label: "0 — Standard User" },
    { value: 1, label: "1 — Arm Only" },
    { value: 2, label: "2 — Guest" },
    { value: 3, label: "3 — Duress" },
    { value: 4, label: "4 — Partition Master (20P only)" },
  ];

  const PARTITION_OPTIONS = [
    { value: "1",   label: "1 — Partition 1 + Common" },
    { value: "2",   label: "2 — Partition 2 + Common" },
    { value: "3",   label: "3 — Common Only" },
    { value: "1,2", label: "1,2 — Partitions 1 & 2 + Common" },
  ];

  /* ── User 01 — Installer ── */
  function renderInstaller($container) {
    const $section = $('<div class="user-section"></div>');
    $section.append(
      '<div class="user-section-header">' +
        '<h4>User 01 — Installer Code</h4>' +
        '<span class="card-hint">Always present. Used to enter programming mode (*99). Cannot be deleted.</span>' +
      '</div>'
    );

    const $row = $('<div class="setting-row"></div>');
    const $input = $('<input type="password" class="user-code-input" placeholder="New code (4 digits)" maxlength="4" autocomplete="off">');
    const $toggle = $('<button class="btn-toggle-vis" title="Show/hide">👁</button>');
    const $saveBtn = $('<button class="btn-save btn-danger">Change Installer Code</button>');
    const $status = $('<span class="save-status"></span>');

    $toggle.on("click", function () {
      $input.attr("type", $input.attr("type") === "password" ? "text" : "password");
    });

    $row.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Change Password"),
        $('<small class="text-danger"></small>').text(
          "⚠ DANGER: If you forget the new installer code you must physically access " +
          "the panel and follow the factory reset procedure described in the manual."
        )
      ),
      $('<div class="setting-control"></div>').append($input, $toggle, $saveBtn, $status)
    );
    $section.append($row);
    $container.append($section);

    $saveBtn.on("click", async function () {
      const newCode = $input.val().trim();
      if (!/^\d{4}$/.test(newCode)) {
        toast("Code must be 4 digits", "err"); return;
      }
      const confirmed = await showConfirmModal({
        icon: "🔐",
        title: "Change Installer Code?",
        body: "<strong>You are about to change the INSTALLER CODE (User 01).</strong><br><br>" +
          "If you forget this code, the only recovery is a physical panel reset " +
          "as described in the installation manual.<br><br>" +
          "The new code will be: <strong>" + newCode + "</strong>",
        okLabel: "Change Code",
        danger: true,
      });
      if (!confirmed) return;

      $saveBtn.prop("disabled", true).addClass("saving");
      $status.text("…").removeClass("ok err");
      lockUI("Changing installer code — waiting for panel…");
      try {
        const res = await api("POST", "/api/user_configure", {
          action: "change_installer",
          new_code: newCode,
        });
        if (res.ok) {
          $status.text("✓").addClass("ok").removeClass("err");
          toast("Installer code changed. Stored code updated.", "ok");
          $input.val("");
        } else {
          throw new Error("Panel did not acknowledge");
        }
      } catch (e) {
        $status.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $saveBtn.prop("disabled", false).removeClass("saving");
      }
    });
  }

  /* ── User 02 — Master ── */
  function renderMaster($container) {
    const $section = $('<div class="user-section"></div>');
    $section.append(
      '<div class="user-section-header">' +
        '<h4>User 02 — Master Code</h4>' +
        '<span class="card-hint">Can add/delete other user codes. Cannot be deleted. Authority level fixed.</span>' +
      '</div>'
    );

    const $row = $('<div class="setting-row"></div>');
    const $input = $('<input type="password" class="user-code-input" placeholder="New code (4 digits)" maxlength="4" autocomplete="off">');
    const $toggle = $('<button class="btn-toggle-vis" title="Show/hide">👁</button>');
    const $saveBtn = $('<button class="btn-save btn-danger">Change Master Code</button>');
    const $status = $('<span class="save-status"></span>');

    $toggle.on("click", function () {
      $input.attr("type", $input.attr("type") === "password" ? "text" : "password");
    });

    $row.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Change Password"),
        $('<small class="text-danger"></small>').text(
          "⚠ DANGER: If you forget the master code, you will need the installer code " +
          "to reset it. The new code is sent twice to the panel for confirmation."
        )
      ),
      $('<div class="setting-control"></div>').append($input, $toggle, $saveBtn, $status)
    );
    $section.append($row);
    $container.append($section);

    $saveBtn.on("click", async function () {
      if (!APP.userCode) { toast("Set master/user code first (🔑 button)", "err"); return; }
      const newCode = $input.val().trim();
      if (!/^\d{4}$/.test(newCode)) {
        toast("Code must be 4 digits", "err"); return;
      }
      const confirmed = await showConfirmModal({
        icon: "🔐",
        title: "Change Master Code?",
        body: "<strong>You are about to change the MASTER CODE (User 02).</strong><br><br>" +
          "The master code is needed to add/delete users and to arm/disarm.<br>" +
          "If you forget it you will need the installer code to reset it.<br><br>" +
          "The new code will be: <strong>" + newCode + "</strong>",
        okLabel: "Change Code",
        danger: true,
      });
      if (!confirmed) return;

      $saveBtn.prop("disabled", true).addClass("saving");
      $status.text("…").removeClass("ok err");
      lockUI("Changing master code — waiting for panel…");
      try {
        const res = await api("POST", "/api/user_configure", {
          action: "change_master",
          new_code: newCode,
        });
        if (res.ok) {
          // Update the stored user code in the frontend
          APP.userCode = newCode;
          $status.text("✓").addClass("ok").removeClass("err");
          toast("Master code changed. Stored code updated.", "ok");
          $input.val("");
        } else {
          throw new Error("Panel did not acknowledge");
        }
      } catch (e) {
        $status.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $saveBtn.prop("disabled", false).removeClass("saving");
      }
    });
  }

  /* ── Users 03–49 ── */
  function renderRegularUsers($container) {
    const $section = $('<div class="user-section"></div>');
    $section.append(
      '<div class="user-section-header">' +
        '<h4>Users 03–49</h4>' +
        '<span class="card-hint">' +
          'Standard users, guests, and duress codes. ' +
          'The panel cannot report which slots are active — operations are write-only.' +
        '</span>' +
      '</div>'
    );

    // User number selector
    const $userRow = $('<div class="setting-row user-select-row"></div>');
    const $userSel = $('<select id="user-num-select"></select>');
    for (let u = 3; u <= 49; u++) {
      const label = `User ${u < 10 ? "0" : ""}${u}`;
      $userSel.append($('<option></option>').val(u).text(label));
    }
    $userRow.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Select User"),
        $('<small></small>').text("Choose a user slot, then use the actions below.")
      ),
      $('<div class="setting-control"></div>').append($userSel)
    );
    $section.append($userRow);

    // --- Grouped actions container ---
    const $groupLabel = $('<div class="user-group-label">Actions for <strong>User 03</strong></div>');
    const $group = $('<div class="user-actions-group"></div>');
    $group.append($groupLabel);

    function padUser(n) { return n < 10 ? "0" + n : String(n); }

    // Update group label when selection changes
    $userSel.on("change", function () {
      const num = parseInt($(this).val(), 10);
      $groupLabel.html('Actions for <strong>User ' + padUser(num) + '</strong>');
    });

    // --- Set / Change Code ---
    const $codeRow = $('<div class="setting-row"></div>');
    const $codeInput = $('<input type="password" class="user-code-input" placeholder="Code (4 digits)" maxlength="4" autocomplete="off">');
    const $codeToggle = $('<button class="btn-toggle-vis" title="Show/hide">👁</button>');
    const $codeBtn = $('<button class="btn-save">Set Code</button>');
    const $codeStatus = $('<span class="save-status"></span>');

    $codeToggle.on("click", function () {
      $codeInput.attr("type", $codeInput.attr("type") === "password" ? "text" : "password");
    });

    $codeRow.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Add / Change Code"),
        $('<small></small>').text("Sets the user's PIN code. If the slot was empty, this creates the user.")
      ),
      $('<div class="setting-control"></div>').append($codeInput, $codeToggle, $codeBtn, $codeStatus)
    );
    $group.append($codeRow);

    $codeBtn.on("click", async function () {
      if (!APP.userCode) { toast("Set master/user code first (🔑 button)", "err"); return; }
      const userNum = parseInt($userSel.val(), 10);
      const newCode = $codeInput.val().trim();
      if (!/^\d{4}$/.test(newCode)) {
        toast("Code must be 4 digits", "err"); return;
      }
      $codeBtn.prop("disabled", true).addClass("saving");
      $codeStatus.text("…").removeClass("ok err");
      lockUI("Setting user " + userNum + " code…");
      try {
        await api("POST", "/api/user_configure", {
          action: "set_code", user_num: userNum, new_code: newCode,
        });
        $codeStatus.text("✓").addClass("ok").removeClass("err");
        toast(`User ${userNum} code set`, "ok");
        $codeInput.val("");
      } catch (e) {
        $codeStatus.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $codeBtn.prop("disabled", false).removeClass("saving");
      }
    });

    // --- Delete User ---
    const $delRow = $('<div class="setting-row"></div>');
    const $delBtn = $('<button class="btn-save btn-danger">Delete User</button>');
    const $delStatus = $('<span class="save-status"></span>');

    $delRow.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Delete User"),
        $('<small></small>').text("Erases the code and all attributes (except assigned partition).")
      ),
      $('<div class="setting-control"></div>').append($delBtn, $delStatus)
    );
    $group.append($delRow);

    $delBtn.on("click", async function () {
      if (!APP.userCode) { toast("Set master/user code first (🔑 button)", "err"); return; }
      const userNum = parseInt($userSel.val(), 10);
      const pn = padUser(userNum);
      const confirmed = await showConfirmModal({
        icon: "🗑️",
        title: "Delete User " + pn + "?",
        body: "This will erase the code and all attributes for <strong>User " + pn + "</strong>.<br><br>" +
          "This action cannot be undone.",
        okLabel: "Delete User",
        danger: true,
      });
      if (!confirmed) return;
      $delBtn.prop("disabled", true).addClass("saving");
      $delStatus.text("…").removeClass("ok err");
      lockUI("Deleting user " + userNum + "…");
      try {
        await api("POST", "/api/user_configure", {
          action: "delete", user_num: userNum,
        });
        $delStatus.text("✓").addClass("ok").removeClass("err");
        toast(`User ${userNum} deleted`, "ok");
      } catch (e) {
        $delStatus.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $delBtn.prop("disabled", false).removeClass("saving");
      }
    });

    // --- Authority Level ---
    const $authRow = $('<div class="setting-row"></div>');
    const $authSel = $('<select class="auth-sel"></select>');
    AUTHORITY_LEVELS.forEach(o => {
      $authSel.append($('<option></option>').val(o.value).text(o.label));
    });
    const $authBtn = $('<button class="btn-save">Set Authority</button>');
    const $authStatus = $('<span class="save-status"></span>');

    $authRow.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("Authority Level"),
        $('<small></small>').text("Controls what the user can do: arm only, full access, duress alert, etc.")
      ),
      $('<div class="setting-control"></div>').append($authSel, $authBtn, $authStatus)
    );
    $group.append($authRow);

    $authBtn.on("click", async function () {
      if (!APP.userCode) { toast("Set master/user code first (🔑 button)", "err"); return; }
      const userNum = parseInt($userSel.val(), 10);
      const level = parseInt($authSel.val(), 10);
      $authBtn.prop("disabled", true).addClass("saving");
      $authStatus.text("…").removeClass("ok err");
      lockUI("Setting authority for user " + userNum + "…");
      try {
        await api("POST", "/api/user_configure", {
          action: "authority", user_num: userNum, authority_level: level,
        });
        $authStatus.text("✓").addClass("ok").removeClass("err");
        toast(`User ${userNum} authority set to ${level}`, "ok");
      } catch (e) {
        $authStatus.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $authBtn.prop("disabled", false).removeClass("saving");
      }
    });

    // --- User Partition ---
    const $partRow = $('<div class="setting-row"></div>');
    const $partSel = $('<select class="part-sel"></select>');
    PARTITION_OPTIONS.forEach(o => {
      $partSel.append($('<option></option>').val(o.value).text(o.label));
    });
    const $partBtn = $('<button class="btn-save">Set Partition</button>');
    const $partStatus = $('<span class="save-status"></span>');

    $partRow.append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text("User Partition (20P)"),
        $('<small></small>').text("Assigns which partition(s) this user can access. Vista-20P only.")
      ),
      $('<div class="setting-control"></div>').append($partSel, $partBtn, $partStatus)
    );
    $group.append($partRow);

    $partBtn.on("click", async function () {
      if (!APP.userCode) { toast("Set master/user code first (🔑 button)", "err"); return; }
      const userNum = parseInt($userSel.val(), 10);
      const parts = $partSel.val().split(",").map(Number);
      $partBtn.prop("disabled", true).addClass("saving");
      $partStatus.text("…").removeClass("ok err");
      lockUI("Setting partition for user " + userNum + "…");
      try {
        await api("POST", "/api/user_configure", {
          action: "partition", user_num: userNum, partitions: parts,
        });
        $partStatus.text("✓").addClass("ok").removeClass("err");
        toast(`User ${userNum} partition set`, "ok");
      } catch (e) {
        $partStatus.text("✗").addClass("err").removeClass("ok");
        toast("Failed: " + (e.message || e), "err");
      } finally {
        unlockUI();
        $partBtn.prop("disabled", false).removeClass("saving");
      }
    });

    $section.append($group);
    $container.append($section);
  }

  /* ── Render all (once) ── */
  let _rendered = false;
  function renderAccounts() {
    if (_rendered) return;
    _rendered = true;
    const $list = $("#accounts-list").empty();
    renderInstaller($list);
    renderMaster($list);
    renderRegularUsers($list);
  }

  // Render when the tab becomes visible
  const $tab = $("#tab-accounts");
  const observer = new MutationObserver(() => {
    if ($tab.hasClass("active")) renderAccounts();
  });
  observer.observe($tab[0], { attributeFilter: ["class"] });

  if ($tab.hasClass("active")) renderAccounts();
});
