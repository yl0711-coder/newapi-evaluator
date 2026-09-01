document.querySelector('#password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  clearError();
  const currentPassword = document.querySelector('#current-password').value;
  const newPassword = document.querySelector('#new-password').value;
  const confirmation = document.querySelector('#confirm-password').value;
  if (newPassword !== confirmation) {
    showError('两次输入的新密码不一致');
    return;
  }
  try {
    await post('/api/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    });
    const notice = document.querySelector('#notice');
    notice.className = 'notice ok';
    notice.textContent = '密码已更新，其他设备上的会话已经失效。';
    event.target.reset();
  } catch (error) {
    showError(error.message);
  }
});
