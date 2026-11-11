/* ===== 表现层：程序绘制飞机和粒子，以振荡器合成音效，不加载素材。 =====
 * 【2026-11-10】渲染不修改战斗状态；关闭声音不会改变游戏推进。
 * 【2026-11-11】连击：倍率跨档时播放上扬音效并震屏；右上角绘制连击数与倒计时条。
 */
(function (root) {
  'use strict';
  const P = root.Plane;
  class Effects {
    /** canvas 为唯一战场；缩放由 CSS 控制，内部按 DPR 提高清晰度。 */
    constructor(canvas) {
      this.canvas = canvas; this.ctx = canvas.getContext('2d'); this.particles = []; this.floaters = []; this.shake = 0; this.muted = false;
      this.reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = P.W * dpr; canvas.height = P.H * dpr; this.ctx.scale(dpr, dpr);
    }
    /** 仅由用户手势调用；没有 Web Audio 的浏览器仍可静音游玩。 */
    unlock() {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return;
      try { if (!this.audio) this.audio = new Audio(); if (this.audio.state === 'suspended') this.audio.resume().catch(e => console.warn('音频恢复失败', e)); }
      catch (e) { console.warn('音频不可用', e); }
    }
    /** 各事件使用短包络，避免每发子弹的音效拖尾和爆音。 */
    sound(kind) {
      if (this.muted || !this.audio || this.audio.state !== 'running') return;
      const presets = { shot: [680, 350, 0.035, 0.012], explosion: [130, 35, 0.18, 0.08], hurt: [180, 60, 0.25, 0.1], bomb: [80, 20, 0.55, 0.14], pickup: [440, 880, 0.16, 0.06], combo: [523, 1046, 0.2, 0.12], over: [250, 55, 0.6, 0.08] };
      const [from, to, duration, volume] = presets[kind], t = this.audio.currentTime;
      const oscillator = this.audio.createOscillator(), gain = this.audio.createGain();
      oscillator.type = kind === 'shot' || kind === 'pickup' || kind === 'combo' ? 'sine' : 'sawtooth';
      oscillator.frequency.setValueAtTime(from, t); oscillator.frequency.exponentialRampToValueAtTime(to, t + duration);
      gain.gain.setValueAtTime(volume, t); gain.gain.exponentialRampToValueAtTime(0.001, t + duration);
      oscillator.connect(gain); gain.connect(this.audio.destination); oscillator.start(t); oscillator.stop(t + duration);
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
    }
    /** 战斗事件携带坐标；降低动态偏好下禁用震屏并减少粒子。 */
    event(kind, entity) {
      this.sound(kind);
      if (kind === 'score') {
        this.floaters.push({ x: entity.x, y: entity.y, life: 0.8, max: 0.8, value: entity.value });
        return;
      }
      if (kind === 'combo') { this.shake = this.reduced ? 0 : 6; return; }
      if (!['explosion', 'hurt', 'bomb', 'pickup'].includes(kind)) return;
      this.shake = this.reduced ? 0 : kind === 'bomb' ? 14 : 5;
      for (let i = 0; i < (this.reduced ? 6 : 24); i++) {
        const a = Math.random() * Math.PI * 2, speed = 30 + Math.random() * 160;
        this.particles.push({ x: entity.x, y: entity.y, vx: Math.cos(a) * speed, vy: Math.sin(a) * speed, life: 0.6, color: kind === 'pickup' ? '#f4c66a' : '#ed7458' });
      }
    }
    /** 所有飞机共享轮廓；敌机翻转，Boss 通过比例与颜色区分。 */
    plane(x, y, size, color, enemy, flash = false) {
      const c = this.ctx; c.save(); c.translate(x, y); if (enemy) c.rotate(Math.PI); c.scale(size, size);
      c.fillStyle = flash ? '#fff' : color;
      c.beginPath(); c.moveTo(0, -28); c.lineTo(8, -6); c.lineTo(29, 13); c.lineTo(29, 19); c.lineTo(7, 12); c.lineTo(6, 25); c.lineTo(13, 30); c.lineTo(-13, 30); c.lineTo(-6, 25); c.lineTo(-7, 12); c.lineTo(-29, 19); c.lineTo(-29, 13); c.lineTo(-8, -6); c.closePath(); c.fill();
      c.fillStyle = '#143b52'; c.fillRect(-3, -11, 6, 15);
      c.fillStyle = '#f4c66a'; c.fillRect(-3, 30, 6, 8 + Math.random() * 7); c.restore();
    }
    /** dt 为真实动画增量；暂停时保持战场与粒子静止。 */
    draw(game, dt, clock) {
      const c = this.ctx, moving = game.state === 'playing';
      c.save(); c.fillStyle = '#123b57'; c.fillRect(0, 0, P.W, P.H);
      if (this.shake) { c.translate((Math.random() - 0.5) * this.shake, (Math.random() - 0.5) * this.shake); this.shake *= 0.88; if (this.shake < 0.1) this.shake = 0; }
      // 云带按游戏时间移动，暂停后不会偷偷改变战场参照物。
      for (let i = 0; i < 11; i++) {
        const y = ((i * 127 + game.time * 32) % 960) - 130, x = (i * 173) % 480;
        c.fillStyle = '#b8d8e509'; c.beginPath(); c.ellipse(x, y, 110, 27, -0.35, 0, Math.PI * 2); c.fill();
      }
      c.strokeStyle = '#c4dfeb0b'; c.setLineDash([7, 24]); c.beginPath(); c.moveTo(240, 0); c.lineTo(240, 720); c.stroke(); c.setLineDash([]);
      for (const b of game.shots) { c.fillStyle = '#f4df9a'; c.fillRect(b.x - 2, b.y - 10, 4, 18); }
      for (const b of game.bullets) { c.fillStyle = '#ed7458'; c.beginPath(); c.arc(b.x, b.y, b.r, 0, Math.PI * 2); c.fill(); c.fillStyle = '#ffe0aa'; c.beginPath(); c.arc(b.x, b.y, 2, 0, Math.PI * 2); c.fill(); }
      for (const e of game.enemies) this.plane(e.x, e.y, e.type === 'boss' ? 2.2 : e.type === 'tank' ? 1.05 : 0.75, e.type === 'boss' ? '#c58873' : '#91a7ac', true, e.flash > 0);
      const p = game.p;
      if (p.inv <= 0 || Math.floor(clock * 12) % 2 === 0 || game.state !== 'playing') this.plane(p.x, p.y, 0.85, '#f3edda', false);
      if (p.shield > 0) { c.strokeStyle = '#86dbdf'; c.lineWidth = 2; c.beginPath(); c.arc(p.x, p.y, 35, 0, Math.PI * 2); c.stroke(); }
      for (const item of game.items) {
        c.save(); c.translate(item.x, item.y); c.fillStyle = '#f4c66a'; c.fillRect(-14, -14, 28, 28); c.fillStyle = '#123b57'; c.textAlign = 'center'; c.font = 'bold 22px sans-serif'; c.fillText({ power: '↑', shield: '◇', heal: '+', bomb: '✦' }[item.type], 0, 8); c.restore();
      }
      for (const particle of this.particles) {
        if (moving) { particle.life -= dt; particle.x += particle.vx * dt; particle.y += particle.vy * dt; }
        c.globalAlpha = Math.max(0, particle.life / 0.6); c.fillStyle = particle.color; c.fillRect(particle.x, particle.y, 4, 4);
      }
      c.globalAlpha = 1; this.particles = this.particles.filter(particle => particle.life > 0);
      // 得分飘字：向上浮动 + 淡出；倍率 ≥2x 金色加粗，数值越大字号越大。
      for (const f of this.floaters) {
        if (moving) f.life -= dt;
        const ratio = f.life / f.max, y = f.y - (1 - ratio) * 48;
        c.globalAlpha = Math.max(0, ratio);
        const high = f.value >= 200;
        c.fillStyle = high ? '#f4c66a' : '#f3edda';
        c.font = `bold ${Math.round(16 + Math.log2(1 + f.value / 100))}px sans-serif`;
        c.textAlign = 'center';
        c.fillText(`${f.value}`, f.x, y);
      }
      c.globalAlpha = 1; this.floaters = this.floaters.filter(f => f.life > 0);
      if (game.boss) { c.fillStyle = '#071e32'; c.fillRect(35, 28, 410, 8); c.fillStyle = '#ed7458'; c.fillRect(35, 28, 410 * Math.max(0, game.boss.hp / game.boss.maxHp), 8); c.fillStyle = '#f3edda'; c.font = '11px sans-serif'; c.fillText('空中堡垒', 35, 21); }
      // 连击：倍率 ≥2x 时金色发光，数字缩放强调倍率变化。
      if (game.combo > 0) {
        const mul = game.comboMul;
        const x = P.W - 70, y = 45;
        const pulse = game.combo % 10 === 0 && game.comboTimer > 2.3 ? 1.4 : 1;
        c.save(); c.translate(x, y); c.scale(pulse, pulse);
        c.textAlign = 'center'; c.textBaseline = 'middle';
        if (mul >= 2) { c.shadowColor = '#f4c66a'; c.shadowBlur = 12; }
        c.fillStyle = mul >= 2 ? '#f4c66a' : '#f3edda';
        c.font = 'bold 24px sans-serif'; c.fillText(`×${mul}`, 0, -2);
        c.shadowBlur = 0; c.fillStyle = '#c0d0d7'; c.font = 'bold 13px sans-serif'; c.fillText(`${game.combo} COMBO`, 0, 15);
        // 倒计时条：越接近结束颜色越红，提醒玩家保持手感。
        const ratio = game.comboTimer / 2.5, barW = 56, barH = 3;
        c.fillStyle = '#071e32'; c.fillRect(-barW / 2, 24, barW, barH);
        c.fillStyle = ratio > 0.4 ? '#f4c66a' : '#ed7458'; c.fillRect(-barW / 2, 24, barW * ratio, barH);
        c.restore();
      }
      c.restore();
    }
  }
  P.Effects = Effects;
})(globalThis);
