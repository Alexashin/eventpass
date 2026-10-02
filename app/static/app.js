(() => {
  document.addEventListener('click', async (event) => {
    const copyButton = event.target.closest('[data-copy-target]');
    if (!copyButton) return;
    const target = document.querySelector(copyButton.dataset.copyTarget);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.value || target.textContent || '');
      const old = copyButton.textContent;
      copyButton.textContent = 'Скопировано';
      setTimeout(() => { copyButton.textContent = old; }, 1200);
    } catch (_) {
      target.focus();
      target.select?.();
    }
  });

  document.addEventListener('submit', (event) => {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
})();
