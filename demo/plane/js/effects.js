/* ===== 表现层：程序绘制飞机和粒子，以振荡器合成音效，不加载素材。 =====
 * 【2026-11-10】渲染不修改战斗状态；关闭声音不会改变游戏推进。
 * 【2026-11-11】连击：倍率跨档时播放上扬音效并震屏；右上角绘制连击数与倒计时条。
 * 擦弹：轻量上扬音效 + 玩家周围蓝色闪光环扩散，提示"贴弹走位成功"。
 * 【2026-11-12】Boss 多阶段：阶段切换发 bossphase 事件，触发震屏 + 音效；
 * Boss 颜色随阶段加深，phaseFlash 期间白色闪烁提示"形态变化"。
 */
(function (root) {
  'use strict';
  const P = root.Plane;
  class Effects {
    /** canvas 为唯一战场；缩放由 CSS 控制，内部按 DPR 提高清晰度。 */
    constructor(canvas) {
      this.canvas = canvas; this.ctx = canvas.getContext('2d'); this.particles = []; this.floaters = []; this.grazes = []; this.shake = 0; this.muted = false; this.bossWarn = 0;
      // 【2026-11-13】时停结束闪光：记录上一帧的 freeze 值，检测从 >0 变为 0 的瞬间。
      // 时停期间战场偏蓝，结束时敌弹突然恢复移动，没有提示容易让玩家措手不及。
      this.prevFreeze = 0; this.freezeFlash = 0;
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
      const presets = { shot: [680, 350, 0.035, 0.012], explosion: [130, 35, 0.18, 0.08], hurt: [180, 60, 0.25, 0.1], bomb: [80, 20, 0.55, 0.14], pickup: [440, 880, 0.16, 0.06], freeze: [880, 1320, 0.3, 0.08], combo: [523, 1046, 0.2, 0.12], graze: [880, 1200, 0.08, 0.06], bossphase: [440, 110, 0.35, 0.16], bosswarn: [660, 220, 0.4, 0.14], over: [250, 55, 0.6, 0.08] };
      // score 等事件没有音效预设，解构 undefined 会抛 TypeError 并中断 rAF 链，必须前置拦截。
      if (!presets[kind]) return;
      const [from, to, duration, volume] = presets[kind], t = this.audio.currentTime;
      const oscillator = this.audio.createOscillator(), gain = this.audio.createGain();
      oscillator.type = kind === 'shot' || kind === 'pickup' || kind === 'combo' || kind === 'graze' ? 'sine' : 'sawtooth';
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
      if (kind === 'graze') { this.grazes.push({ x: entity.x, y: entity.y, life: 0.35, max: 0.35 }); return; }
      if (kind === 'combo') { this.shake = this.reduced ? 0 : 6; return; }
      if (kind === 'bossphase') { this.shake = this.reduced ? 0 : 10; return; }
      if (kind === 'bosswarn') { this.bossWarn = entity.time; return; }
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
      // 【2026-11-13】检测时停结束：上一帧 freeze>0 且本帧归零，触发白色闪光。
      // 放在 draw 开头是因为 game.update 已经改了 freeze，此时才能准确判断"刚好结束"。
      if (this.prevFreeze > 0 && game.p.freeze <= 0) this.freezeFlash = 0.25;
      this.prevFreeze = game.p.freeze;
      if (this.freezeFlash > 0 && moving) this.freezeFlash -= dt;
      c.save(); c.fillStyle = '#123b57'; c.fillRect(0, 0, P.W, P.H);
      if (this.shake) { c.translate((Math.random() - 0.5) * this.shake, (Math.random() - 0.5) * this.shake); this.shake *= 0.88; if (this.shake < 0.1) this.shake = 0; }
      // 【2026-11-13】双层视差：远云更淡、更慢，近云更实、更快，形成前后景纵深。
      // 两层都按 game.time 驱动，暂停后不会偷偷改变战场参照物（与下方注释的约定一致）。
      // 远云用更小的横向步长和半速滚动，避免两层云的相位重复导致"看起来像同一层"。
      for (let i = 0; i < 8; i++) {
        const y = ((i * 163 + game.time * 16) % 960) - 130, x = (i * 211) % 480;
        c.fillStyle = '#b8d8e505'; c.beginPath(); c.ellipse(x, y, 140, 34, -0.22, 0, Math.PI * 2); c.fill();
      }
      for (let i = 0; i < 11; i++) {
        const y = ((i * 127 + game.time * 32) % 960) - 130, x = (i * 173) % 480;
        c.fillStyle = '#b8d8e509'; c.beginPath(); c.ellipse(x, y, 110, 27, -0.35, 0, Math.PI * 2); c.fill();
      }
      c.strokeStyle = '#c4dfeb0b'; c.setLineDash([7, 24]); c.beginPath(); c.moveTo(240, 0); c.lineTo(240, 720); c.stroke(); c.setLineDash([]);
      for (const b of game.shots) { c.fillStyle = '#f4df9a'; c.fillRect(b.x - 2, b.y - 10, 4, 18); }
      // 【2026-11-13】普通敌弹批处理：用 Path2D 合并所有同色圆，一次 fill 完成，
      // 避免 Boss 密集弹幕时每发子弹都单独 beginPath/fill 造成 draw call 过多掉帧。
      // 追踪弹颜色和光晕不同，合并收益低，保持单独绘制。
      const normal = [];
      for (const b of game.bullets) if (!b.track) normal.push(b);
      if (normal.length) {
        // Path2D.arc() 不会自动开启新子路径：连续调用 arc() 时，后一个圆会用直线
        // 连到前一个圆的终点，所有子弹串成一张网，fill() 就把网围成的区域整片涂满，
        // 形成大片黄色。每次 arc() 前先 moveTo 到圆起点，让每个圆成为独立子路径。
        const outer = new Path2D(), inner = new Path2D();
        for (const b of normal) { outer.moveTo(b.x + b.r, b.y); outer.arc(b.x, b.y, b.r, 0, Math.PI * 2); inner.moveTo(b.x + 2, b.y); inner.arc(b.x, b.y, 2, 0, Math.PI * 2); }
        c.fillStyle = '#ed7458'; c.fill(outer);
        c.fillStyle = '#ffe0aa'; c.fill(inner);
      }
      for (const b of game.bullets) {
        if (b.track) {
          // 【2026-11-12】追踪弹紫色光晕 + 更大核心 + 短尾迹，与普通敌弹明显区分。
          c.fillStyle = 'rgba(146,96,180,0.28)'; c.beginPath(); c.arc(b.x, b.y, b.r + 6, 0, Math.PI * 2); c.fill();
          c.fillStyle = '#b07de0'; c.beginPath(); c.arc(b.x, b.y, b.r, 0, Math.PI * 2); c.fill();
          c.fillStyle = '#ffe0ff'; c.beginPath(); c.arc(b.x, b.y, 3, 0, Math.PI * 2); c.fill();
          // 尾迹：沿速度方向反方向画短线，提示追踪弹的移动方向。
          const sp = Math.hypot(b.vx, b.vy) || 1; c.strokeStyle = 'rgba(176,125,224,0.5)'; c.lineWidth = 2;
          c.beginPath(); c.moveTo(b.x, b.y); c.lineTo(b.x - b.vx / sp * 12, b.y - b.vy / sp * 12); c.stroke();
        }
      }
      // 【2026-11-13】时停视觉提示：敌弹整体叠一层淡蓝半透明，与玩家自己仍能移动、射击形成对比，
      // 让玩家一眼看出"现在处于时停、敌弹被冻住"，而不是靠 HUD 文字去猜剩余时间。
      if (game.p.freeze > 0) {
        c.fillStyle = 'rgba(134, 219, 223, 0.16)';
        for (const b of game.bullets) { c.beginPath(); c.arc(b.x, b.y, b.r + 5, 0, Math.PI * 2); c.fill(); }
      }
      // 【2026-11-13】时停全屏色调：敌弹静止时叠一层极淡的青蓝，覆盖整个战场。
      // 与上方敌弹高亮是两层——高亮标记"哪些弹被冻住"，全屏色调标记"现在处于时停"。
      // 透明度随剩余时间略微增强，最后一秒最明显，暗示"快结束了"。
      if (game.p.freeze > 0) {
        const freezeRatio = game.p.freeze / 4;
        c.fillStyle = `rgba(100, 200, 220, ${0.04 + freezeRatio * 0.06})`;
        c.fillRect(-20, -20, P.W + 40, P.H + 40);
      }
      // Boss 三阶段配色：暖棕 → 橙红 → 深红狂暴，phaseFlash 期间整体闪白。
      const bossColor = { 1: '#c58873', 2: '#b5654a', 3: '#8a3b2e' };
      for (const e of game.enemies) {
        const isBoss = e.type === 'boss';
        const isSuicide = e.type === 'suicide';
        this.plane(e.x, e.y, isBoss ? 2.2 : e.type === 'tank' ? 1.05 : 0.75, isBoss ? bossColor[e.bossPhase || 1] : isSuicide ? '#d44a3f' : '#91a7ac', true, e.flash > 0 || (isBoss && e.phaseFlash > 0));
      }
      const p = game.p;
      if (p.inv <= 0 || Math.floor(clock * 12) % 2 === 0 || game.state !== 'playing') this.plane(p.x, p.y, 0.85, '#f3edda', false);
      // 【2026-11-13】引擎尾焰：玩家尾部橙黄小三角，用 game.time 驱动闪烁频率。
      // 用 game.time 而非 clock——暂停时 game.time 冻结，尾焰也随之静止，
      // 与"暂停时战场静止"的整体约定一致；高度用确定性值而非 Math.random，
      // 避免每帧随机高度导致尾焰抖动过快、看起来像噪点。
      if (game.state === 'playing') {
        const flameLen = 10 + (Math.floor(game.time * 30) % 2) * 5;
        c.fillStyle = '#f4c66a';
        c.beginPath(); c.moveTo(p.x - 4, p.y + 22); c.lineTo(p.x, p.y + 22 + flameLen); c.lineTo(p.x + 4, p.y + 22); c.closePath(); c.fill();
      }
      if (p.shield > 0) { c.strokeStyle = '#86dbdf'; c.lineWidth = 2; c.beginPath(); c.arc(p.x, p.y, 35, 0, Math.PI * 2); c.stroke(); }
      // 擦弹闪光环：从玩家位置向外扩散，半径随剩余生命比例增长，蓝色描边淡出。
      for (const g of this.grazes) {
        if (moving) g.life -= dt;
        const ratio = g.life / g.max;
        const radius = 18 + (1 - ratio) * 34;
        c.globalAlpha = Math.max(0, ratio) * 0.8;
        c.strokeStyle = '#86dbdf'; c.lineWidth = 2;
        c.beginPath(); c.arc(g.x, g.y, radius, 0, Math.PI * 2); c.stroke();
      }
      c.globalAlpha = 1; this.grazes = this.grazes.filter(g => g.life > 0);
      for (const item of game.items) {
        // 【2026-11-13】道具脉冲缩放：让下落的补给有"闪烁"感，更易被注意到。
        // 用 item.age 驱动——暂停时 age 不更新，脉冲自然冻结，与战场静止约定一致。
        const pulse = 1 + Math.sin(item.age * 6) * 0.08;
        c.save(); c.translate(item.x, item.y); c.scale(pulse, pulse); c.fillStyle = '#f4c66a'; c.fillRect(-14, -14, 28, 28); c.fillStyle = '#123b57'; c.textAlign = 'center'; c.font = 'bold 22px sans-serif'; c.fillText({ power: '↑', shield: '◇', heal: '+', bomb: '✦', time: '⏱' }[item.type], 0, 8); c.restore();
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
      if (game.boss) { c.fillStyle = '#071e32'; c.fillRect(35, 28, 410, 8); c.fillStyle = '#ed7458'; c.fillRect(35, 28, 410 * Math.max(0, game.boss.hp / game.boss.maxHp), 8);
        // 【2026-11-13】分段竖线：与 game.js 里 66% / 33% 的阶段判定阈值严格对齐，
        // 让玩家能从血量条直接读出"还差几发进下一阶段"，避免"条没变色但阶段已切"的割裂感。
        // 用背景色 #071e32 画在橙红条上形成深槽，不描边——高 DPR 下细描边容易糊成一团。
        for (const ratio of [0.66, 0.33]) c.fillStyle = '#071e32', c.fillRect(35 + 410 * ratio, 28, 2, 8);
        c.fillStyle = '#f3edda'; c.font = '11px sans-serif'; c.fillText('空中堡垒', 35, 21); }
      // 【2026-11-12】Boss 出场预警：顶部红色闪烁警报条，随剩余时间递减。
      if (this.bossWarn > 0) {
        if (moving) this.bossWarn -= dt;
        const ratio = Math.max(0, this.bossWarn / 3);
        const blink = Math.sin(this.bossWarn * 12) > 0;
        c.globalAlpha = ratio * (blink ? 0.9 : 0.25);
        c.fillStyle = '#ed3b3b'; c.fillRect(0, 0, P.W, 6);
        c.fillStyle = '#ffe0e0'; c.font = 'bold 16px sans-serif'; c.textAlign = 'center';
        c.fillText('⚠ 空中堡垒接近中 ⚠', P.W / 2, 34);
        c.globalAlpha = 1;
      }
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
      // 【2026-11-13】时停结束闪光：全屏白色半透明，0.25 秒内淡出，提示敌弹已恢复移动。
      if (this.freezeFlash > 0) {
        c.fillStyle = `rgba(255, 255, 255, ${this.freezeFlash / 0.25 * 0.25})`;
        c.fillRect(0, 0, P.W, P.H);
      }
      c.restore();
    }
  }
  P.Effects = Effects;
})(globalThis);
