// Refresh odds link — show spinner
document.addEventListener('DOMContentLoaded', () => {
  const refreshLink = document.getElementById('refreshOddsLink');
  if (refreshLink) {
    refreshLink.addEventListener('click', (e) => {
      e.preventDefault();
      refreshLink.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Refreshing...';
      fetch('/odds/refresh')
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            refreshLink.innerHTML = '<i class="bi bi-check-circle"></i> Odds Updated';
            setTimeout(() => {
              refreshLink.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Refresh Odds';
            }, 3000);
          } else {
            refreshLink.innerHTML = '<i class="bi bi-exclamation-circle"></i> Error';
            setTimeout(() => {
              refreshLink.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Refresh Odds';
            }, 3000);
          }
        })
        .catch(() => {
          refreshLink.innerHTML = '<i class="bi bi-exclamation-circle"></i> Error';
        });
    });
  }
});
