// static/js/main.js
// مسؤول عن إرسال الطلبات إلى /account/add_step1 و /account/add_step2
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('addAccountForm');
  const phoneInput = document.getElementById('phone');
  const apiIdInput = document.getElementById('api_id');
  const apiHashInput = document.getElementById('api_hash');
  const sendBtn = document.getElementById('sendCodeBtn');
  const step2 = document.getElementById('step2');
  const codeInput = document.getElementById('code');
  const pass2fa = document.getElementById('password2fa');
  const verifyBtn = document.getElementById('verifyBtn');
  const msg = document.getElementById('message');

  function showMessage(text, isError=false){
    msg.textContent = text;
    msg.style.color = isError ? 'crimson' : 'green';
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    sendBtn.disabled = true;
    showMessage('جاري إرسال طلب الكود...');
    const payload = {
      phone: phoneInput.value.trim(),
      api_id: apiIdInput.value.trim(),
      api_hash: apiHashInput.value.trim()
    };
    try {
      const res = await fetch('/account/add_step1', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      if (res.status === 401) {
        // غير مصرح - إعادة توجيه لتسجيل الدخول
        showMessage('يرجى تسجيل الدخول أولاً', true);
        setTimeout(()=> window.location.href = '/login', 1000);
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        showMessage(data.msg || 'فشل إرسال الكود', true);
        sendBtn.disabled = false;
        return;
      }
      showMessage(data.msg || 'تم إرسال الكود، افحص رسائلك.');
      step2.style.display = 'block';
    } catch (err) {
      showMessage('خطأ في الاتصال: ' + err.message, true);
    } finally {
      sendBtn.disabled = false;
    }
  });

  verifyBtn.addEventListener('click', async () => {
    verifyBtn.disabled = true;
    showMessage('جاري التحقق...');
    const payload = {
      phone: phoneInput.value.trim(),
      code: codeInput.value.trim(),
      password: pass2fa.value.trim() || undefined
    };
    try {
      const res = await fetch('/account/add_step2', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      if (res.status === 401) {
        showMessage('يرجى تسجيل الدخول أولاً', true);
        setTimeout(()=> window.location.href = '/login', 1000);
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        showMessage(data.msg || 'فشل التحقق', true);
        verifyBtn.disabled = false;
        return;
      }
      if (data.status === 'need_password') {
        showMessage('المستخدم يطلب كلمة مرور ثانية (2FA). ضعها في الحقل ثم اضغط تحقق.', true);
        verifyBtn.disabled = false;
        return;
      }
      if (data.status === 'success') {
        showMessage('تم إضافة الحساب بنجاح — تم تشغيله في الرادار.');
        // إعادة تحميل الصفحة لجلب الحسابات المحدثة
        setTimeout(()=> location.reload(), 1000);
        return;
      }
      showMessage('استجابة غير متوقعة: ' + JSON.stringify(data), true);
    } catch (err) {
      showMessage('خطأ في الاتصال: ' + err.message, true);
    } finally {
      verifyBtn.disabled = false;
    }
  });
});
