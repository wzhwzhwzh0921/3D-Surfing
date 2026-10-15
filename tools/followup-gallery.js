(() => {
  'use strict';
  const all = [...document.querySelectorAll('.case')];
  const controls = Object.fromEntries(['mode','environment','search','answers','previous','next','result-count','page-number','empty'].map(id => [id, document.getElementById(id)]));
  const params = new URLSearchParams(location.search);
  const perPage = 8;
  let page = Math.max(0, Number.parseInt(params.get('page') || '1', 10) - 1) || 0;
  for (const key of ['mode','environment']) {
    const value = params.get(key);
    if ([...controls[key].options].some(o => o.value === value)) controls[key].value = value;
  }
  controls.search.value = params.get('scene') || params.get('q') || '';
  const searchable = new Map(all.map(card => [card, `${card.dataset.uid} ${card.textContent}`.toLocaleLowerCase()]));
  function filter() {
    const q = controls.search.value.trim().toLocaleLowerCase();
    return all.filter(card =>
      (controls.mode.value === 'all' || card.dataset.mode === controls.mode.value) &&
      (controls.environment.value === 'all' || card.dataset.environment === controls.environment.value) &&
      (!q || searchable.get(card).includes(q)));
  }
  function hydrate(card) {
    card.querySelectorAll('img[data-src]').forEach(img => {
      img.src = img.dataset.src;
      img.loading = 'lazy';
      img.decoding = 'async';
      img.removeAttribute('data-src');
    });
    const current = card.querySelector('.reveal').getAttribute('src');
    card.querySelectorAll('.thumb').forEach(button => {
      const selected = button.dataset.view === current;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  function render(writeURL = true) {
    const selected = filter();
    const pageCount = Math.max(1, Math.ceil(selected.length / perPage));
    page = Math.min(page, pageCount - 1);
    all.forEach(card => { card.hidden = true; });
    const visible = selected.slice(page * perPage, (page + 1) * perPage);
    visible.forEach(card => { card.hidden = false; hydrate(card); });
    const first = selected.length ? page * perPage + 1 : 0;
    const last = Math.min((page + 1) * perPage, selected.length);
    controls['result-count'].textContent = `匹配 ${selected.length} 题 · 当前 ${first}–${last} · 每页 ${perPage} 题`;
    controls['page-number'].textContent = `${page + 1} / ${pageCount} 页`;
    controls.previous.disabled = page === 0;
    controls.next.disabled = page + 1 >= pageCount;
    controls.empty.hidden = selected.length !== 0;
    document.body.classList.toggle('hide-answers', !controls.answers.checked);
    if (writeURL) {
      const query = new URLSearchParams();
      if (controls.mode.value !== 'all') query.set('mode', controls.mode.value);
      if (controls.environment.value !== 'all') query.set('environment', controls.environment.value);
      if (controls.search.value.trim()) query.set('q', controls.search.value.trim());
      if (page) query.set('page', String(page + 1));
      history.replaceState(null, '', location.pathname + (query.size ? '?' + query : '') + location.hash);
    }
  }
  function revealHash() {
    if (!location.hash) return;
    const id = decodeURIComponent(location.hash.slice(1));
    const target = all.find(card => card.id === id);
    if (!target) return;
    controls.mode.value = 'all';
    controls.environment.value = 'all';
    controls.search.value = '';
    page = Math.floor(all.indexOf(target) / perPage);
    render();
    target.scrollIntoView({block: 'start'});
  }
  ['mode','environment'].forEach(key => controls[key].addEventListener('change', () => { page = 0; render(); }));
  controls.search.addEventListener('input', () => { page = 0; render(); });
  controls.answers.addEventListener('change', () => render(false));
  const turn = delta => {
    page += delta;
    render();
    document.querySelector('.toolbar').scrollIntoView({block: 'start'});
  };
  controls.previous.addEventListener('click', () => turn(-1));
  controls.next.addEventListener('click', () => turn(1));
  const dialog = document.getElementById('image-dialog');
  document.getElementById('cases').addEventListener('click', event => {
    const thumb = event.target.closest('.thumb');
    if (thumb) {
      const card = thumb.closest('.case');
      const image = card.querySelector('.reveal');
      image.src = thumb.dataset.view;
      card.querySelector('.reveal-caption').textContent = thumb.dataset.caption;
      card.querySelectorAll('.thumb').forEach(button => {
        button.classList.toggle('selected', button === thumb);
        button.setAttribute('aria-pressed', String(button === thumb));
      });
      return;
    }
    if (event.target.matches('.large-image')) {
      document.getElementById('dialog-image').src = event.target.src;
      document.getElementById('dialog-caption').textContent = event.target.closest('figure').querySelector('figcaption').textContent;
      dialog.showModal();
    }
  });
  document.getElementById('close-dialog').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  addEventListener('hashchange', revealHash);
  render(false);
  revealHash();
})();
