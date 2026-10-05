(function () {
  'use strict';
  function $id(x){ return document.getElementById(x); }

  // Открыть форму оценки для конкретного отчёта (кнопки 👍/👎 в таблице истории).
  function openFeedback(reportKey, query){
    query = query ? decodeURIComponent(query) : '';
    $id('ff-key').textContent = decodeURIComponent(reportKey);
    $id('ff-query').textContent = '«' + query + '»';
    $id('ff-status').textContent = '';
    // Подхватываем email из поля «Ваш email для отчёта» (report-email), если он
    // задан — чтобы не заставлять человека вводить его ещё раз. Только если
    // поле формы отзыва ещё пусто (не затираем то, что пользователь начал вводить).
    var reportEmail = $id('report-email');
    var ffEmail = $id('ff-email');
    if (reportEmail && reportEmail.value.trim() && ffEmail && !ffEmail.value.trim()) {
      ffEmail.value = reportEmail.value.trim();
    }
    $id('feedback-form').style.display = '';
  }

  // Отправить оценку (up/down) или текстовый отзыв (кнопка "Написать отзыв").
  // Имя/email НЕобязательны: если человек ввёл email — привязываем его к отзыву,
  // если нет — сохраняем реакцию анонимной (см. feedback.save_feedback).
  async function submitFeedback(rating){
    var key=$id('ff-key').textContent, name=$id('ff-name').value.trim(),
        email=$id('ff-email').value.trim(), comment=$id('ff-comment').value.trim(),
        st=$id('ff-status');
    // Только текстовый отзыв требует комментария. 👍/👎 можно без имени/email.
    if(rating==='text' && !comment){ st.textContent='Напишите комментарий.'; return; }
    st.textContent='Сохраняем…';
    try{
      var r=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({report_key:key,name:name||null,email:email||null,rating:rating,comment:comment})});
      if(!r.ok){ var e=await r.json().catch(function(){return{};}); throw new Error(e.detail||('Ошибка '+r.status)); }
      st.textContent='Спасибо за отзыв! '+(rating==='up'?'👍':(rating==='down'?'👎':'💬'));
      $id('ff-comment').value = '';
      $id('feedback-form').style.display='none';
    }catch(err){ st.textContent='Ошибка: '+err.message; }
  }

  // Мгновенная оценка из истории запросов: клик по эмодзи 👍/👎 сразу пишет
  // оценку в feedback.db (как ссылки из письма) — без открытия отдельной формы.
  // name/email необязательны, поэтому анонимная оценка тоже сохраняется.
  async function rateReport(reportKey, rating, evt){
    var anchor = (evt || event) && (evt || event).currentTarget;
    var st;
    if(anchor){ st = anchor.querySelector('.rate-status') || anchor.appendChild(Object.assign(document.createElement('span'),{className:'rate-status',style:'font-size:13px;margin-left:6px'})); }
    try{
      var r=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({report_key:reportKey,name:null,email:null,rating:rating,comment:''})});
      if(!r.ok){ var e=await r.json().catch(function(){return{};}); throw new Error(e.detail||('Ошибка '+r.status)); }
      if(st){ st.textContent=' ✓'; st.title='Оценка сохранена'; }
    }catch(err){
      if(st){ st.textContent=' ✕'; st.title=err.message; }
    }
  }

  // Панель "💬 Отзывы": показать/скрыть список собранных отзывов.
  async function loadFeedback(){
    var panel=$id('feedback-panel'), tb=document.querySelector('#feedback-table tbody'), empty=$id('feedback-empty');
    if(!panel) return;
    if(panel.style.display!=='none'){ panel.style.display='none'; return; }
    var rows=await (await fetch('/api/feedback')).json();
    tb.innerHTML=''; empty.style.display=rows.length?'none':'';
    rows.forEach(function(f){
      var rating=f.rating==='up'?'👍':(f.rating==='down'?'👎':'💬');
      var tr=document.createElement('tr');
      tr.innerHTML='<td>'+(f.query||'')+'</td><td>'+(f.name||'')+'</td><td>'+(f.email||'')+'</td><td>'+rating+'</td><td>'+(f.comment||'')+'</td><td class="muted">'+(f.created_at?new Date(f.created_at).toLocaleString('ru-RU'):'')+'</td>';
      tb.appendChild(tr);
    });
    panel.style.display='';
  }

  function init(){
    var b=$id('feedback-btn'); if(b) b.addEventListener('click',loadFeedback);
    var up=$id('ff-up'), dn=$id('ff-down'), tx=$id('ff-text');
    if(up) up.addEventListener('click',function(){submitFeedback('up');});
    if(dn) dn.addEventListener('click',function(){submitFeedback('down');});
    if(tx) tx.addEventListener('click',function(){submitFeedback('text');});
  }
  window.openFeedback=openFeedback;
  window.rateReport=rateReport;
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded',init); }else{ init(); }
})();
