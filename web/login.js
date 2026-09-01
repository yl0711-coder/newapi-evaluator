const form = document.querySelector('#login-form');
const notice = document.querySelector('#notice');
const button = document.querySelector('#login-button');

function safeNext() {
  const value = new URLSearchParams(location.search).get('next') || '/index.html';
  return value.startsWith('/') && !value.startsWith('//') ? value : '/index.html';
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  notice.classList.add('hide');
  button.disabled = true;
  button.textContent = '登录中…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: form.username.value,
        password: form.password.value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `登录失败（HTTP ${response.status}）`);
    location.replace(safeNext());
  } catch (error) {
    notice.textContent = error.message;
    notice.classList.remove('hide');
    button.disabled = false;
    button.textContent = '登录';
  }
});
