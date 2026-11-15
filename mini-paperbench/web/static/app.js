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
      try {
        const r = await fetch(live.dataset.url, { cache: "no-store" });
        if (r.ok) live.innerHTML = await r.text();
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

  // 새 작업: 논문 출처에 맞는 입력만 보인다
  const form = document.getElementById("new-job");
  if (form) {
    const sync = () => {
      const v = form.querySelector("input[name=source]:checked").value;
      form.querySelectorAll("[data-show]").forEach((el) => { el.hidden = el.dataset.show !== v; });
    };
    form.querySelectorAll("input[name=source]").forEach((el) => el.addEventListener("change", sync));
    sync();
    form.addEventListener("submit", () => {
      const b = form.querySelector("button[type=submit]");
      b.disabled = true;
      b.textContent = "접수하는 중...";
    });
  }

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
