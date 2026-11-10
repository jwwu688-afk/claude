/* ===== 浏览器入口：连接输入、战斗状态与 DOM，不让渲染决定游戏规则。 =====
 * 【2026-11-10】经典脚本可以直接双击打开；无需服务器或构建步骤。
 */
(function () {
  'use strict';
  const P = window.Plane, $ = id => document.getElementById(id);
  const canvas = $('canvas'), effects = new P.Effects(canvas);
  const game = new P.Game(Math.random, (kind, entity) => effects.event(kind, entity));
  const keys = new Set(); let target = null, pointer = null, previous = 0, best = 0, shownState = '';
  // 隐私模式可能拒绝 localStorage；成绩保存失败不应中断游戏。
  try { best = Math.max(0, Number(localStorage.getItem('plane-best')) || 0); } catch (e) { console.warn('无法读取最高分', e); }
  /** 所有离开操作都清空输入，防止切回窗口后仍然朝一个方向移动。 */
  function clearInput() { keys.clear(); target = null; pointer = null; }
  /** 开始或继续由同一按钮触发，只有重新开局才重建核心状态。 */
  function start() {
    effects.unlock(); clearInput();
    if (game.state === 'paused') game.pause(); else { game.reset(); effects.particles = []; }
    canvas.focus(); sync();
  }
  /** 结算时才写存储；每帧只刷新数字，状态变化才改遮罩。 */
  function sync() {
    $('score').textContent = String(game.score).padStart(6, '0');
    $('lives').textContent = `${Math.max(0, game.p.lives)} / 3`;
    $('wave').textContent = String(game.level).padStart(2, '0');
    $('bombs').textContent = game.p.bombs; $('best').textContent = String(best).padStart(6, '0');
    $('bomb').disabled = game.state !== 'playing' || !game.p.bombs;
    $('pause').disabled = !['playing', 'paused'].includes(game.state);
    $('pause').firstChild.textContent = game.state === 'paused' ? '继续' : '暂停';
    $('power').textContent = [game.p.power > 0 ? `${game.p.weapon} 发火力 ${Math.ceil(game.p.power)}s` : '', game.p.shield > 0 ? `护盾 ${Math.ceil(game.p.shield)}s` : ''].filter(Boolean).join('　/　');
    if (shownState === game.state) return;
    shownState = game.state; $('overlay').hidden = game.state === 'playing';
    if (game.state === 'ready') return;
    const paused = game.state === 'paused';
    $('title').textContent = paused ? '暂歇' : '归航';
    $('message').textContent = paused ? '天空会等你。准备好后继续出击。' : `本次得分 ${game.score} · 坚持 ${Math.floor(game.time)} 秒`;
    $('start').textContent = paused ? '继续飞行' : '再次出击';
    $('hint').textContent = paused ? '按 P 也可继续' : '每一次归航，都是下一次起飞的开始';
    if (!paused && game.score > best) {
      best = game.score;
      try { localStorage.setItem('plane-best', String(best)); } catch (e) { console.warn('无法保存最高分', e); }
      $('best').textContent = String(best).padStart(6, '0');
    }
  }
  /** 暂停切换后清理输入，触摸与键盘保持一致。 */
  function pause() { game.pause(); clearInput(); sync(); }
  $('start').addEventListener('click', start);
  $('pause').addEventListener('click', pause);
  $('bomb').addEventListener('click', () => { effects.unlock(); game.bomb(); sync(); });
  $('sound').addEventListener('click', () => {
    effects.unlock(); effects.muted = !effects.muted;
    $('sound').textContent = `音效：${effects.muted ? '关' : '开'}`;
    $('sound').setAttribute('aria-pressed', String(effects.muted));
  });
  const movement = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'KeyW', 'KeyA', 'KeyS', 'KeyD'];
  window.addEventListener('keydown', event => {
    if (movement.includes(event.code) || event.code === 'Space') {
      if (event.target instanceof HTMLButtonElement && game.state !== 'playing') return;
      event.preventDefault();
    }
    if (movement.includes(event.code)) keys.add(event.code);
    if (event.repeat) return;
    if (event.code === 'KeyP' || event.code === 'Escape') pause();
    if (event.code === 'Space' && game.state === 'playing') { effects.unlock(); game.bomb(); }
  });
  window.addEventListener('keyup', event => keys.delete(event.code));
  /** rect 是 CSS 像素，转换到固定战场坐标后与 DPR 无关。 */
  function point(event) {
    const rect = canvas.getBoundingClientRect();
    target = { x: (event.clientX - rect.left) / rect.width * P.W, y: (event.clientY - rect.top) / rect.height * P.H };
  }
  canvas.tabIndex = 0;
  canvas.addEventListener('pointerdown', event => {
    if (game.state !== 'playing' || pointer !== null) return;
    effects.unlock(); pointer = event.pointerId; canvas.setPointerCapture(pointer); point(event);
  });
  canvas.addEventListener('pointermove', event => { if (event.pointerId === pointer) point(event); });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) canvas.addEventListener(name, event => { if (event.pointerId === pointer) { pointer = null; target = null; } });
  /** 不自动继续，避免用户回来尚未看清就损失生命。 */
  function suspend() { clearInput(); if (game.state === 'playing') { game.pause(); sync(); } }
  window.addEventListener('blur', suspend);
  document.addEventListener('visibilitychange', () => { if (document.hidden) suspend(); });
  /** 帧增量由核心再次设上限；暂停后也更新基准时间，恢复不积累延迟。 */
  function frame(timestamp) {
    const dt = previous ? Math.min((timestamp - previous) / 1000, 0.05) : 0; previous = timestamp;
    game.update(dt, { x: Number(keys.has('ArrowRight') || keys.has('KeyD')) - Number(keys.has('ArrowLeft') || keys.has('KeyA')), y: Number(keys.has('ArrowDown') || keys.has('KeyS')) - Number(keys.has('ArrowUp') || keys.has('KeyW')), target });
    effects.draw(game, dt, timestamp / 1000); sync(); requestAnimationFrame(frame);
  }
  sync(); requestAnimationFrame(frame);
})();
