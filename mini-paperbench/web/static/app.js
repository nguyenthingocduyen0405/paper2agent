// PaperAgent 화면 동작: 진행 상황 갱신, 양식 전환, 결과 표 거르기
(function () {
  // 작업 상세: 진행 중이면 3초마다 상태 부분만 다시 받는다
  const live = document.getElementById("live");
  if (live) {
    const running = () => {
      const s = live.querySelector("[data-status]");
      return s && s.dataset.status === "running";
    };
    const poll = async () => {
      if (!running()) return;
      const wasRunning = running();
      try {
        const r = await fetch(live.dataset.url, { cache: "no-store" });
        if (r.ok) {
          // 펼쳐 둔 목록(채점 기준 등)은 새로 받아도 펼친 채로 둔다
          const open = [...live.querySelectorAll("details[id]")].filter((d) => d.open).map((d) => d.id);
          live.innerHTML = await r.text();
          open.forEach((id) => { const d = document.getElementById(id); if (d) d.open = true; });
          // 논문 준비가 끝나 채점 기준이 막 생겼으면 한 번 새로 고쳐 채점 기준 전체를 보여 준다
          const s = live.querySelector("[data-status]");
          if (s && s.dataset.rubric === "1" && !document.getElementById("rubric-card")) {
            location.reload();
            return;
          }
          // 작업(또는 GPU 실행)이 막 끝났으면 한 번 새로 고친다: 실행 여부를 묻는 창과 최종 결과를 보여 준다
          if (wasRunning && !running()) {
            location.reload();
            return;
          }
        }
      } catch (e) {
        // 네트워크가 잠깐 끊겨도 다음 차례에 다시 받는다
      }
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  }

  // 작업 목록: 진행 중인 작업이 있으면 10초마다 새로 고친다
  if (document.querySelector("[data-autorefresh]")) {
    setTimeout(() => location.reload(), 10000);
  }
  document.querySelectorAll("tr[data-href]").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (!e.target.closest("a")) location.href = tr.dataset.href;
    });
  });

  // 새 작업: 논문 파일을 끌어다 놓거나 눌러서 고른다
  const drop = document.getElementById("drop");
  if (drop) {
    const input = drop.querySelector("input[type=file]");
    const name = drop.querySelector(".drop-name");
    const show = () => {
      const f = input.files[0];
      name.textContent = f ? `${f.name} (${(f.size / 1024 / 1024).toFixed(1)} MB)` : "";
      drop.classList.toggle("has-file", !!f);
    };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        show();
      }
    });
    show();
  }
  const form = document.getElementById("new-job");
  if (form) {
    form.addEventListener("submit", () => {
      const b = form.querySelector("button[type=submit]");
      b.disabled = true;
      b.textContent = "접수하는 중...";
    });
  }

  // 실행 기록은 마지막 줄이 보이게 아래로 내려 둔다(#live를 다시 받아도)
  const toEnd = () => document.querySelectorAll("pre[data-end]").forEach((p) => { p.scrollTop = p.scrollHeight; });
  toEnd();
  if (live) new MutationObserver(toEnd).observe(live, { childList: true });

  // 코드 작성과 검증이 끝나면 GPU에서 바로 실행할지 묻는다
  const ask = document.getElementById("gpu-ask");
  if (ask && typeof ask.showModal === "function") ask.showModal();

  // 되돌리기 어려운 요청(GPU 실행, 취소)은 한 번 더 묻는다. #live 안의 폼도 있으므로 문서에 건다
  document.addEventListener("submit", (e) => {
    const f = e.target.closest("form[data-confirm]");
    if (f && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });

  // 결과 표 거르기. #live 내용이 바뀌어도 동작하도록 문서에 한 번만 건다
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-filter]");
    if (!btn) return;
    const card = btn.closest(".card");
    card.querySelectorAll("[data-filter]").forEach((b) => b.classList.toggle("active", b === btn));
    card.querySelectorAll("tr[data-score]").forEach((tr) => {
      tr.hidden = !(btn.dataset.filter === "all" || tr.dataset.score === btn.dataset.filter);
    });
  });
})();

// 미리 받기: speculation rules를 모르는 브라우저(Firefox, Safari)는 마우스를 올린 링크를 <link rel=prefetch>로 받아 둔다
(() => {
  if (window.HTMLScriptElement && HTMLScriptElement.supports && HTMLScriptElement.supports("speculationrules")) return;
  const skip = /\/(code\.zip|report\.md|files\/|gpu\/|api\/)/;
  const done = new Set();
  let timer = null;
  document.addEventListener("mouseover", (e) => {
    const a = e.target.closest && e.target.closest("a[href^='/']");
    if (!a || a.hasAttribute("download") || a.target === "_blank" || skip.test(a.pathname) || done.has(a.href)) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      done.add(a.href);
      const link = document.createElement("link");
      link.rel = "prefetch";
      link.href = a.href;
      document.head.appendChild(link);
    }, 80);
  }, { passive: true });
})();
