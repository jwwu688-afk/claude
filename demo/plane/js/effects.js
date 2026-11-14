/* ===== 表现层：程序绘制金属质感飞机、能量弹幕、多层粒子，以振荡器合成音效。 =====
 * 【2026-11-14 全面改造】整个渲染层重写为雷霆战机（Raiden）街机风格：
 *   - 背景：深空径向渐变 + 彩色星云 + 三层辉光星点
 *   - 飞机：金属渐变机身 + 引擎光焰 + 青色辉光描边
 *   - 敌机：按类型分色 + 发光红色核心 + 外层辉光
 *   - 子弹：玩家能量弹带拖尾辉光，敌弹为发光球体
 *   - 爆炸：多层粒子 + 扩散光晕环
 *   - 道具/护盾/连击：全部加辉光效果
 * 渲染不修改战斗状态；关闭声音不会改变游戏推进。
 * 连击：倍率跨档时播放上扬音效并震屏；右上角绘制连击数与倒计时条。
 * 擦弹：轻量上扬音效 + 玩家周围蓝色闪光环扩散，提示"贴弹走位成功"。
 * Boss 多阶段：阶段切换发 bossphase 事件，触发震屏 + 音效；
 * Boss 颜色随阶段加深，phaseFlash 期间白色闪烁提示"形态变化"。
 */
(function (root) {
  'use strict';
  const P = root.Plane;
  class Effects {
    /** canvas 为唯一战场；缩放由 CSS 控制，内部按 DPR 提高清晰度。 */
    constructor(canvas) {
      this.canvas = canvas; this.ctx = canvas.getContext('2d'); this.particles = []; this.floaters = []; this.grazes = []; this.shake = 0; this.muted = false; this.bossWarn = 0;
      // 时停结束闪光：记录上一帧的 freeze 值，检测从 >0 变为 0 的瞬间。
      this.prevFreeze = 0; this.freezeFlash = 0;
      // Boss 阶段切换波纹：从 Boss 位置向外扩散的白色环形波，比单纯震屏更有仪式感。
      this.bossWave = 0;
      // 受伤红屏：受击瞬间叠加红色，0.3 秒内淡出，比单纯震屏更能传达"被打了"。
      this.hurtFlash = 0;
      // 炸弹白光：释放炸弹时全屏白色闪光，0.4 秒内淡出，强化"清屏"的爽感。
      this.bombFlash = 0;
      // 时停冰晶：时停期间在战场中漂浮的冰蓝色六角形晶体，强化"冻结"的氛围。
      this.iceCrystals = [];
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
    sound(kind, pitchMul = 1) {
      if (this.muted || !this.audio || this.audio.state !== 'running') return;
      const presets = { shot: [680, 350, 0.035, 0.012], explosion: [130, 35, 0.18, 0.08], hurt: [180, 60, 0.25, 0.1], bomb: [80, 20, 0.55, 0.14], pickup: [440, 880, 0.16, 0.06], freeze: [880, 1320, 0.3, 0.08], combo: [523, 1046, 0.2, 0.12], graze: [880, 1200, 0.08, 0.06], bossphase: [440, 110, 0.35, 0.16], bosswarn: [660, 220, 0.4, 0.14], over: [250, 55, 0.6, 0.08] };
      // score 等事件没有音效预设，解构 undefined 会抛 TypeError 并中断 rAF 链，必须前置拦截。
      if (!presets[kind]) return;
      // pitchMul 让连击音效的起止频率随倍率上移，倍率越高音调越亮，听觉上传递"连击火热"。
      const [from, to, duration, volume] = presets[kind], t = this.audio.currentTime;
      const f0 = from * pitchMul, f1 = to * pitchMul;
      const oscillator = this.audio.createOscillator(), gain = this.audio.createGain();
      oscillator.type = kind === 'shot' || kind === 'pickup' || kind === 'combo' || kind === 'graze' ? 'sine' : 'sawtooth';
      oscillator.frequency.setValueAtTime(f0, t); oscillator.frequency.exponentialRampToValueAtTime(f1, t + duration);
      gain.gain.setValueAtTime(volume, t); gain.gain.exponentialRampToValueAtTime(0.001, t + duration);
      oscillator.connect(gain); gain.connect(this.audio.destination); oscillator.start(t); oscillator.stop(t + duration);
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
    }
    /** 战斗事件携带坐标；降低动态偏好下禁用震屏并减少粒子。 */
    event(kind, entity) {
      // 连击音效音调随倍率递增：倍率 2x 时音调升 1.1 倍，3x 时 1.25 倍…
      // 听觉上传递"连击火热"，比视觉数字更直觉。
      if (kind === 'combo' && entity && entity.combo) {
        const mul = 1 + Math.floor(entity.combo / 10);
        this.sound('combo', 1 + (mul - 1) * 0.12);
      } else {
        this.sound(kind);
      }
      if (kind === 'score') {
        this.floaters.push({ x: entity.x, y: entity.y, life: 0.8, max: 0.8, value: entity.value });
        return;
      }
      if (kind === 'graze') { this.grazes.push({ x: entity.x, y: entity.y, life: 0.35, max: 0.35 }); return; }
      if (kind === 'combo') { this.shake = this.reduced ? 0 : 6; return; }
      if (kind === 'bossphase') { this.shake = this.reduced ? 0 : 10; this.bossWave = 0.6; this.bossWaveX = entity.x; this.bossWaveY = entity.y; return; }
      if (kind === 'bosswarn') { this.bossWarn = entity.time; return; }
      // 【2026-11-14】Boss 击毁：触发全屏白光 + 强震屏，配合 game 的慢动作形成仪式感。
      if (kind === 'bossdeath') { this.bombFlash = 0.6; this.shake = this.reduced ? 0 : 18; return; }
      if (!['explosion', 'hurt', 'bomb', 'pickup'].includes(kind)) return;
      this.shake = this.reduced ? 0 : kind === 'bomb' ? 14 : 5;
      // 受伤红屏 + 炸弹白光：在震屏之外加一层全屏颜色反馈，强化"被打"和"清屏"的体感。
      if (kind === 'hurt') this.hurtFlash = 0.3;
      if (kind === 'bomb') this.bombFlash = 0.4;
      // 多层粒子：爆炸用三层不同颜色和速度，形成"碎片+火花+烟"的层次感。
      const count = this.reduced ? 6 : (kind === 'bomb' ? 40 : 24);
      for (let i = 0; i < count; i++) {
        const a = Math.random() * Math.PI * 2, speed = 30 + Math.random() * (kind === 'bomb' ? 220 : 160);
        // 外层碎片：橙红；内层火花：黄白；最内层烟：暗红
        const layer = Math.random();
        const color = layer < 0.5 ? '#ed7458' : (layer < 0.8 ? '#f4c66a' : '#a8503a');
        this.particles.push({ x: entity.x, y: entity.y, vx: Math.cos(a) * speed, vy: Math.sin(a) * speed, life: 0.6 + Math.random() * 0.3, max: 0.9, color, size: 3 + Math.random() * 4 });
      }
      // 爆炸光晕环：从爆炸中心扩散的圆形光晕，模拟冲击波。
      this.particles.push({ x: entity.x, y: entity.y, vx: 0, vy: 0, life: 0.35, max: 0.35, color: kind === 'pickup' ? '#f4c66a' : '#ed7458', size: 0, ring: true });
    }

    /** 绘制金属渐变飞机。
     * 【2026-11-14】雷霆战机风格：机身用线性渐变模拟金属曲面，外层加青色辉光描边，
     * 引擎处画多层光焰。敌机翻转并按类型着色，Boss 用更大尺寸和多部件。 */
    plane(x, y, size, baseColor, enemy, flash = false, isBoss = false) {
      const c = this.ctx;
      c.save(); c.translate(x, y); if (enemy) c.rotate(Math.PI); c.scale(size, size);

      // 辉光描边：先画一层半透明的亮色光晕，再画机身，营造能量感。
      if (!flash) {
        c.shadowColor = enemy ? 'rgba(237, 80, 60, 0.5)' : 'rgba(80, 200, 255, 0.5)';
        c.shadowBlur = isBoss ? 24 : 12;
      }

      // 机身金属渐变：从亮色到暗色的线性渐变，模拟金属曲面反光。
      const grad = c.createLinearGradient(-29, -28, 29, 30);
      if (flash) {
        grad.addColorStop(0, '#ffffff'); grad.addColorStop(1, '#e0e0e0');
      } else {
        // 玩家战机：亮银灰→深蓝灰，体现金属质感
        if (!enemy) { grad.addColorStop(0, '#e8edf2'); grad.addColorStop(0.4, '#c0c8d0'); grad.addColorStop(0.7, '#9aa6b0'); grad.addColorStop(1, '#6a7882'); }
        // 敌机：按 baseColor 做亮→暗渐变
        else { grad.addColorStop(0, baseColor); grad.addColorStop(0.5, baseColor); grad.addColorStop(1, '#1a0a08'); }
      }
      c.fillStyle = grad;
      c.beginPath(); c.moveTo(0, -28); c.lineTo(8, -6); c.lineTo(29, 13); c.lineTo(29, 19); c.lineTo(7, 12); c.lineTo(6, 25); c.lineTo(13, 30); c.lineTo(-13, 30); c.lineTo(-6, 25); c.lineTo(-7, 12); c.lineTo(-29, 19); c.lineTo(-29, 13); c.lineTo(-8, -6); c.closePath(); c.fill();

      c.shadowBlur = 0;
      // 机身金属边线：深色描边让平面色块有轮廓。
      c.strokeStyle = '#0a2230'; c.lineWidth = 1.5; c.stroke();

      if (!flash) {
        // 座舱高光：青蓝色半透明椭圆，模拟座舱玻璃的反光。
        c.fillStyle = 'rgba(120, 200, 220, 0.6)';
        c.beginPath(); c.ellipse(0, -14, 4, 8, 0, 0, Math.PI * 2); c.fill();
        c.fillStyle = 'rgba(220, 250, 255, 0.8)';
        c.beginPath(); c.ellipse(0, -16, 2, 3.5, 0, 0, Math.PI * 2); c.fill();
        // 机翼边缘灯：红色（敌机）或青色（玩家），模拟战机翼尖航行灯。
        if (!enemy) { c.fillStyle = 'rgba(80, 200, 255, 0.9)'; c.beginPath(); c.arc(-29, 16, 2, 0, Math.PI * 2); c.fill(); c.beginPath(); c.arc(29, 16, 2, 0, Math.PI * 2); c.fill(); }
        else { c.fillStyle = 'rgba(237, 80, 60, 0.9)'; c.beginPath(); c.arc(-29, 16, 2, 0, Math.PI * 2); c.fill(); c.beginPath(); c.arc(29, 16, 2, 0, Math.PI * 2); c.fill(); }
      }

      // 引擎尾焰基座
      c.fillStyle = '#143b52'; c.fillRect(-3, -11, 6, 15);
      c.restore();
    }

    /** 绘制引擎光焰：多层渐变火焰，雷霆战机风格的推进器效果。 */
    engineFlame(c, x, y, baseLen, time, w = 4) {
      const len = baseLen + (Math.floor(time * 30) % 2) * (baseLen * 0.3);
      // 外层：橙红，最宽最暗
      c.fillStyle = 'rgba(237, 80, 60, 0.5)';
      c.beginPath(); c.moveTo(x - w, y); c.lineTo(x, y + len * 1.6); c.lineTo(x + w, y); c.closePath(); c.fill();
      // 中层：橙黄
      c.fillStyle = 'rgba(244, 198, 106, 0.8)';
      c.beginPath(); c.moveTo(x - w * 0.6, y); c.lineTo(x, y + len); c.lineTo(x + w * 0.6, y); c.closePath(); c.fill();
      // 内层：白黄，最窄最亮
      c.fillStyle = 'rgba(255, 245, 208, 1)';
      c.beginPath(); c.moveTo(x - w * 0.3, y); c.lineTo(x, y + len * 0.5); c.lineTo(x + w * 0.3, y); c.closePath(); c.fill();
    }

    /** 绘制各类型敌机造型：每种敌机有独立轮廓，不是同一个飞机翻转。
     * 【2026-11-14】scout 是尖角飞翼、weaver 是菱形、tank 是重型六边形、
     * suicide 是自爆球。形状差异让玩家一眼能分辨威胁类型，提升街机感。 */
    drawEnemy(c, e, baseColor, flash) {
      const s = e.type === 'tank' ? 1.05 : 0.75;
      c.save(); c.translate(e.x, e.y); c.rotate(Math.PI); c.scale(s, s);
      // 敌机辉光描边
      if (!flash) { c.shadowColor = 'rgba(237, 80, 60, 0.5)'; c.shadowBlur = 12; }
      const grad = c.createLinearGradient(-30, -30, 30, 30);
      if (flash) { grad.addColorStop(0, '#fff'); grad.addColorStop(1, '#e0e0e0'); }
      else { grad.addColorStop(0, baseColor); grad.addColorStop(0.5, baseColor); grad.addColorStop(1, '#1a0a08'); }
      c.fillStyle = grad; c.strokeStyle = '#0a2230'; c.lineWidth = 1.5;

      if (e.type === 'scout') {
        // 侦察机：尖角飞翼，速度快、体积小
        c.beginPath(); c.moveTo(0, -22); c.lineTo(10, -2); c.lineTo(22, 16); c.lineTo(8, 16); c.lineTo(6, 20); c.lineTo(-6, 20); c.lineTo(-8, 16); c.lineTo(-22, 16); c.lineTo(-10, -2); c.closePath(); c.fill(); c.stroke();
      } else if (e.type === 'weaver') {
        // 织网机：菱形，左右摇摆发射弹幕
        c.beginPath(); c.moveTo(0, -20); c.lineTo(20, 0); c.lineTo(0, 22); c.lineTo(-20, 0); c.closePath(); c.fill(); c.stroke();
      } else if (e.type === 'tank') {
        // 重装机：六边形，体积大、血量高
        c.beginPath();
        for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3 - Math.PI / 2; const x = Math.cos(a) * 26, y = Math.sin(a) * 26; if (i === 0) c.moveTo(x, y); else c.lineTo(x, y); }
        c.closePath(); c.fill(); c.stroke();
        // 装甲板纹理
        c.fillStyle = 'rgba(0,0,0,0.3)'; c.fillRect(-10, -6, 20, 4);
      } else if (e.type === 'suicide') {
        // 自爆机：圆球体，向玩家直冲
        c.beginPath(); c.arc(0, 0, 18, 0, Math.PI * 2); c.fill(); c.stroke();
        // 自爆警告纹：旋转的红色弧
        if (!flash) { c.strokeStyle = 'rgba(237, 80, 60, 0.6)'; c.lineWidth = 2; c.beginPath(); c.arc(0, 0, 12, e.phase, e.phase + Math.PI * 1.2); c.stroke(); }
      } else {
        // 兜底：用通用飞机轮廓
        c.beginPath(); c.moveTo(0, -28); c.lineTo(8, -6); c.lineTo(29, 13); c.lineTo(29, 19); c.lineTo(7, 12); c.lineTo(6, 25); c.lineTo(13, 30); c.lineTo(-13, 30); c.lineTo(-6, 25); c.lineTo(-7, 12); c.lineTo(-29, 19); c.lineTo(-29, 13); c.lineTo(-8, -6); c.closePath(); c.fill(); c.stroke();
      }

      c.shadowBlur = 0;
      // 红色能量核心：每种敌机都有，标识弱点位置
      if (!flash) {
        c.fillStyle = 'rgba(237, 80, 60, 0.7)';
        c.beginPath(); c.arc(0, 0, 4, 0, Math.PI * 2); c.fill();
        c.fillStyle = 'rgba(255, 200, 180, 0.5)';
        c.beginPath(); c.arc(0, 0, 2, 0, Math.PI * 2); c.fill();
      }
      c.restore();
    }

    /** 绘制 Boss：多部件造型，中央核心 + 两侧炮塔 + 装甲板。
     * 【2026-11-14】Boss 不再是放大版敌机，而是独立的空中堡垒造型：
     * 中央六边形核心（阶段切换时变色发光）、两侧炮塔（阶段越高越展开）、
     * 外层装甲板。阶段 3 时核心脉动发红光，体现狂暴状态。 */
    drawBoss(c, e, baseColor, flash) {
      const c2 = this.ctx;
      c2.save(); c2.translate(e.x, e.y);
      const phase = e.bossPhase || 1;

      // 辉光：阶段越高辉光越强
      if (!flash) { c2.shadowColor = phase === 3 ? 'rgba(237, 40, 30, 0.7)' : 'rgba(237, 80, 60, 0.5)'; c2.shadowBlur = 24; }

      // 外层装甲板：大块深色装甲，先画再被核心覆盖
      c2.fillStyle = 'rgba(20, 25, 30, 0.9)'; c2.strokeStyle = '#0a2230'; c2.lineWidth = 2;
      c2.beginPath(); c2.moveTo(-75, -25); c2.lineTo(-50, -40); c2.lineTo(50, -40); c2.lineTo(75, -25); c2.lineTo(80, 20); c2.lineTo(55, 40); c2.lineTo(-55, 40); c2.lineTo(-80, 20); c2.closePath();
      c2.fill(); c2.stroke();

      // 两侧炮塔：阶段 3 时向外展开更多
      const turretSpread = phase === 1 ? 0 : phase === 2 ? 8 : 16;
      c2.shadowBlur = 0;
      const turretGrad = c2.createLinearGradient(0, -20, 0, 30);
      turretGrad.addColorStop(0, flash ? '#e0e0e0' : baseColor);
      turretGrad.addColorStop(1, '#2a1010');
      c2.fillStyle = turretGrad; c2.strokeStyle = '#0a2230'; c2.lineWidth = 1.5;
      // 左炮塔
      c2.beginPath(); c2.ellipse(-55 - turretSpread, 0, 18, 14, 0, 0, Math.PI * 2); c2.fill(); c2.stroke();
      // 右炮塔
      c2.beginPath(); c2.ellipse(55 + turretSpread, 0, 18, 14, 0, 0, Math.PI * 2); c2.fill(); c2.stroke();
      // 炮口：炮塔中央的黑色圆
      c2.fillStyle = '#0a0a0a';
      c2.beginPath(); c2.arc(-55 - turretSpread, 0, 5, 0, Math.PI * 2); c2.fill();
      c2.beginPath(); c2.arc(55 + turretSpread, 0, 5, 0, Math.PI * 2); c2.fill();

      // 中央核心：六边形能量核心，阶段越高越亮
      const corePulse = phase === 3 ? 1 + Math.sin(e.age * 8) * 0.08 : 1;
      c2.save(); c2.scale(corePulse, corePulse);
      const coreGrad = c2.createRadialGradient(0, -5, 2, 0, -5, 30);
      if (flash) { coreGrad.addColorStop(0, '#fff'); coreGrad.addColorStop(1, '#ccc'); }
      else if (phase === 3) { coreGrad.addColorStop(0, '#ff6050'); coreGrad.addColorStop(0.5, '#c83020'); coreGrad.addColorStop(1, '#501008'); }
      else if (phase === 2) { coreGrad.addColorStop(0, '#e88050'); coreGrad.addColorStop(0.5, '#b5654a'); coreGrad.addColorStop(1, '#401810'); }
      else { coreGrad.addColorStop(0, '#d0a080'); coreGrad.addColorStop(0.5, '#c58873'); coreGrad.addColorStop(1, '#301810'); }
      c2.fillStyle = coreGrad; c2.strokeStyle = '#0a2230'; c2.lineWidth = 2;
      c2.beginPath();
      for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3 - Math.PI / 2; const x = Math.cos(a) * 30, y = Math.sin(a) * 30 - 5; if (i === 0) c2.moveTo(x, y); else c2.lineTo(x, y); }
      c2.closePath(); c2.fill(); c2.stroke();
      // 核心内发光点
      if (!flash) { c2.fillStyle = 'rgba(255, 200, 180, 0.6)'; c2.beginPath(); c2.arc(0, -5, 8, 0, Math.PI * 2); c2.fill(); c2.fillStyle = 'rgba(255, 240, 220, 0.8)'; c2.beginPath(); c2.arc(0, -5, 4, 0, Math.PI * 2); c2.fill(); }
      c2.restore();

      // 阶段 3 时装甲板缝隙发光
      if (phase === 3 && !flash) {
        c2.strokeStyle = 'rgba(237, 40, 30, 0.4)'; c2.lineWidth = 1;
        c2.beginPath(); c2.moveTo(-75, -25); c2.lineTo(-50, -40); c2.stroke();
        c2.beginPath(); c2.moveTo(75, -25); c2.lineTo(50, -40); c2.stroke();
        c2.beginPath(); c2.moveTo(-80, 20); c2.lineTo(-55, 40); c2.stroke();
        c2.beginPath(); c2.moveTo(80, 20); c2.lineTo(55, 40); c2.stroke();
      }

      c2.restore();
    }

    /** dt 为真实动画增量；暂停时保持战场与粒子静止。 */
    draw(game, dt, clock) {
      const c = this.ctx, moving = game.state === 'playing';
      // 检测时停结束：上一帧 freeze>0 且本帧归零，触发白色闪光。
      if (this.prevFreeze > 0 && game.p.freeze <= 0) this.freezeFlash = 0.25;
      this.prevFreeze = game.p.freeze;
      if (this.freezeFlash > 0 && moving) this.freezeFlash -= dt;
      c.save();
      // 深空径向渐变背景：从中心亮蓝到边缘深黑，营造太空纵深感。
      const bgGrad = c.createRadialGradient(P.W / 2, P.H / 2, 100, P.W / 2, P.H / 2, 500);
      bgGrad.addColorStop(0, '#1a3a5c'); bgGrad.addColorStop(0.4, '#0d2238'); bgGrad.addColorStop(1, '#030a14');
      c.fillStyle = bgGrad; c.fillRect(0, 0, P.W, P.H);
      // Boss 阶段3红色氛围：狂暴阶段整个战场被红色光晕笼罩，配合阶段波纹和核心脉动，
      // 传递"Boss 已经进入最危险的形态"的压迫感，比仅靠 Boss 自身变色更强烈。
      if (game.boss && game.boss.bossPhase === 3 && !this.reduced) {
        const bossRed = c.createRadialGradient(game.boss.x, game.boss.y, 60, game.boss.x, game.boss.y, 380);
        bossRed.addColorStop(0, 'rgba(237, 40, 30, 0.12)');
        bossRed.addColorStop(1, 'rgba(237, 40, 30, 0)');
        c.fillStyle = bossRed; c.fillRect(0, 0, P.W, P.H);
        // 战场边缘也叠一层淡红，与暗角形成"血色围困"的感觉。
        c.fillStyle = 'rgba(120, 20, 15, 0.12)'; c.fillRect(0, 0, P.W, P.H);
      }
      if (this.shake) { c.translate((Math.random() - 0.5) * this.shake, (Math.random() - 0.5) * this.shake); this.shake *= 0.88; if (this.shake < 0.1) this.shake = 0; }

      // ===== 星云层：彩色半透明椭圆，缓慢滚动，模拟太空星云 =====
      // 用蓝紫色和暗红色两层星云交错，增加太空的色彩层次。
      for (let i = 0; i < 5; i++) {
        const y = ((i * 237 + game.time * 8) % 960) - 130, x = (i * 173) % P.W;
        c.fillStyle = 'rgba(60, 100, 180, 0.04)';
        c.beginPath(); c.ellipse(x, y, 180, 50, -0.3, 0, Math.PI * 2); c.fill();
      }
      for (let i = 0; i < 3; i++) {
        const y = ((i * 311 + game.time * 5) % 960) - 130, x = (i * 271 + 120) % P.W;
        c.fillStyle = 'rgba(100, 40, 80, 0.03)';
        c.beginPath(); c.ellipse(x, y, 150, 45, 0.25, 0, Math.PI * 2); c.fill();
      }

      // ===== 三层星点视差：远/中/近三层，近处大亮、远处小暗 =====
      // 远层：小暗、慢速，模拟深空背景星。
      for (let i = 0; i < 40; i++) {
        const y = ((i * 47 + game.time * 25) % 960) - 130, x = (i * 131 + Math.sin(game.time * 0.3 + i) * 8) % P.W;
        const tw = 0.6 + Math.sin(game.time * 1.2 + i * 0.7) * 0.4;
        c.fillStyle = `rgba(180, 200, 220, ${0.12 * tw})`;
        c.beginPath(); c.arc(x, y, 1.0, 0, Math.PI * 2); c.fill();
      }
      // 中层：中等亮度、中速，带微弱闪烁。
      for (let i = 0; i < 25; i++) {
        const y = ((i * 89 + game.time * 60) % 960) - 130, x = (i * 197 + Math.cos(game.time * 0.5 + i) * 10) % P.W;
        const tw = 0.7 + Math.sin(game.time * 2 + i * 1.1) * 0.3;
        c.fillStyle = `rgba(220, 235, 250, ${0.35 * tw})`;
        c.beginPath(); c.arc(x, y, 1.6, 0, Math.PI * 2); c.fill();
      }
      // 近层：大亮、快速，偶尔加暖色星，用径向渐变产生辉光。
      for (let i = 0; i < 14; i++) {
        const y = ((i * 151 + game.time * 110) % 960) - 130, x = (i * 233 + Math.sin(game.time * 0.7 + i) * 14) % P.W;
        const isWarm = i % 5 === 0;
        const grad = c.createRadialGradient(x, y, 0, x, y, 4);
        grad.addColorStop(0, isWarm ? 'rgba(255, 220, 160, 0.8)' : 'rgba(240, 250, 255, 0.8)');
        grad.addColorStop(1, 'rgba(240, 250, 255, 0)');
        c.fillStyle = grad; c.beginPath(); c.arc(x, y, 4, 0, Math.PI * 2); c.fill();
      }
      c.shadowBlur = 0;

      // 中央虚线导航轴：淡淡的青色虚线，提供空间参考。
      c.strokeStyle = 'rgba(80, 180, 220, 0.06)'; c.setLineDash([7, 24]); c.beginPath(); c.moveTo(240, 0); c.lineTo(240, 720); c.stroke(); c.setLineDash([]);

      // ===== 玩家子弹：能量弹 + 拖尾辉光 =====
      // 【2026-11-14】火力等级视觉进化：等级 1 青色细弹、等级 2 金色加宽、等级 3 白蓝大弹，
      // 让玩家从弹道外观直观感受到火力升级，而不是只靠数字变化。
      const lv = game.p.weapon;
      const colors = {
        1: { glow: 'rgba(80, 200, 255, 0.25)', body: '#a0e8ff', core: '#ffffff', trail: 'rgba(120, 220, 255, 0.5)', w: 4, h: 22 },
        2: { glow: 'rgba(244, 198, 106, 0.25)', body: '#f4c66a', core: '#ffffff', trail: 'rgba(244, 198, 106, 0.5)', w: 6, h: 26 },
        3: { glow: 'rgba(160, 220, 255, 0.35)', body: '#d0f0ff', core: '#ffffff', trail: 'rgba(180, 240, 255, 0.6)', w: 8, h: 30 },
      };
      const col = colors[lv] || colors[1];
      for (const b of game.shots) {
        const sp = Math.hypot(b.vx, b.vy) || 1;
        // 【2026-11-14】三种武器的渲染差异：
        // 散射弹：能量弹 + 拖尾渐变（当前已实现）。
        // 激光：细长光束，无拖尾，用渐变矩形模拟能量束，更宽更亮。
        // 导弹：圆弹体 + 尾焰粒子效果，带旋转。
        if (b.shotType === 'laser') {
          // 激光：细长矩形，从弹体到尾部渐变，模拟能量束。
          const len = 28;
          const lg = c.createLinearGradient(b.x, b.y - len, b.x, b.y + 4);
          lg.addColorStop(0, 'rgba(180, 240, 255, 0)');
          lg.addColorStop(0.6, col.body);
          lg.addColorStop(1, '#ffffff');
          c.fillStyle = lg; c.fillRect(b.x - col.w / 2, b.y - len, col.w, len + 4);
          c.fillStyle = col.glow; c.fillRect(b.x - col.w / 2 - 1, b.y - len, col.w + 2, len + 4);
        } else if (b.shotType === 'missile') {
          // 导弹：圆弹体 + 尾焰。
          const ang = Math.atan2(b.vy, b.vx);
          const flameLen = 14;
          // 尾焰：三角形渐变。
          const fx = b.x - Math.cos(ang - Math.PI / 2) * 0 - Math.cos(ang) * flameLen;
          const fy = b.y - Math.sin(ang - Math.PI / 2) * 0 - Math.sin(ang) * flameLen;
          const fg = c.createLinearGradient(b.x, b.y, fx, fy);
          fg.addColorStop(0, 'rgba(255, 200, 80, 0.8)');
          fg.addColorStop(1, 'rgba(255, 100, 40, 0)');
          c.fillStyle = fg;
          c.beginPath(); c.moveTo(b.x - 3, b.y); c.lineTo(fx, fy); c.lineTo(b.x + 3, b.y); c.closePath(); c.fill();
          // 弹体。
          c.fillStyle = col.glow; c.beginPath(); c.arc(b.x, b.y, 5, 0, Math.PI * 2); c.fill();
          c.fillStyle = col.body; c.beginPath(); c.arc(b.x, b.y, 3, 0, Math.PI * 2); c.fill();
          c.fillStyle = col.core; c.beginPath(); c.arc(b.x, b.y, 1.5, 0, Math.PI * 2); c.fill();
        } else {
          // 散射弹：拖尾渐变 + 弹体（原有实现）。
          const tx = b.x - b.vx / sp * 16, ty = b.y - 10 - b.vy / sp * 16;
          const trailGrad = c.createLinearGradient(b.x, b.y - 10, tx, ty);
          trailGrad.addColorStop(0, col.trail);
          trailGrad.addColorStop(1, 'rgba(120, 220, 255, 0)');
          c.fillStyle = trailGrad; c.lineWidth = 3;
          c.beginPath(); c.moveTo(b.x - 3, b.y - 10); c.lineTo(b.x + 3, b.y - 10);
          c.lineTo(tx + 2, ty); c.lineTo(tx - 2, ty); c.closePath(); c.fill();
          // 弹体辉光：半透明矩形替代 shadowBlur，减少每帧开销。
          c.fillStyle = col.glow; c.fillRect(b.x - col.w / 2, b.y - col.h / 2 - 1, col.w, col.h);
          c.fillStyle = col.body; c.fillRect(b.x - col.w / 4, b.y - col.h / 2 + 2, col.w / 2, col.h - 4);
          // 弹芯：白色亮心。
          c.fillStyle = col.core; c.fillRect(b.x - 1, b.y - col.h / 2 + 4, 2, col.h - 8);
        }
      }

      // ===== 敌弹：发光球体（普通+追踪） =====
      // 光晕用纯半透明圆叠加，不用 shadowBlur——密集弹幕时 shadowBlur 每帧开销大，
      // 而半透明圆叠加视觉效果相近，帧率明显更稳。
      const normal = [];
      for (const b of game.bullets) if (!b.track) normal.push(b);
      if (normal.length) {
        c.fillStyle = 'rgba(237, 80, 60, 0.2)';
        for (const b of normal) { c.beginPath(); c.arc(b.x, b.y, b.r + 5, 0, Math.PI * 2); c.fill(); }
        // 弹体：橙红色发光球。
        const outer = new Path2D(), inner = new Path2D();
        for (const b of normal) { outer.moveTo(b.x + b.r, b.y); outer.arc(b.x, b.y, b.r, 0, Math.PI * 2); inner.moveTo(b.x + 2, b.y); inner.arc(b.x, b.y, 2, 0, Math.PI * 2); }
        c.fillStyle = '#ed5040'; c.fill(outer);
        c.fillStyle = '#ffe0aa'; c.fill(inner);
      }
      for (const b of game.bullets) {
        if (b.track) {
          // 追踪弹：紫色大光晕 + 核心 + 尾迹。
          c.fillStyle = 'rgba(160, 80, 220, 0.2)'; c.beginPath(); c.arc(b.x, b.y, b.r + 8, 0, Math.PI * 2); c.fill();
          c.fillStyle = 'rgba(160, 80, 220, 0.3)'; c.beginPath(); c.arc(b.x, b.y, b.r + 4, 0, Math.PI * 2); c.fill();
          c.fillStyle = '#b07de0'; c.beginPath(); c.arc(b.x, b.y, b.r, 0, Math.PI * 2); c.fill();
          c.fillStyle = '#ffe0ff'; c.beginPath(); c.arc(b.x, b.y, 3, 0, Math.PI * 2); c.fill();
          const sp = Math.hypot(b.vx, b.vy) || 1;
          c.strokeStyle = 'rgba(176, 125, 224, 0.4)'; c.lineWidth = 2;
          c.beginPath(); c.moveTo(b.x, b.y); c.lineTo(b.x - b.vx / sp * 14, b.y - b.vy / sp * 14); c.stroke();
        }
      }

      // 时停视觉提示：敌弹叠蓝色半透明，标记被冻住的弹。
      if (game.p.freeze > 0) {
        c.fillStyle = 'rgba(134, 219, 223, 0.16)';
        for (const b of game.bullets) { c.beginPath(); c.arc(b.x, b.y, b.r + 5, 0, Math.PI * 2); c.fill(); }
        // 全屏时停色调。
        const freezeRatio = game.p.freeze / 4;
        c.fillStyle = `rgba(100, 200, 220, ${0.04 + freezeRatio * 0.06})`;
        c.fillRect(-20, -20, P.W + 40, P.H + 40);
        // 生成冰晶：时停期间每帧补充，营造"冰封战场"的氛围。
        if (moving && this.iceCrystals.length < 12 && Math.random() < 0.15) {
          this.iceCrystals.push({
            x: Math.random() * P.W, y: -10,
            vx: (Math.random() - 0.5) * 20, vy: 30 + Math.random() * 30,
            size: 3 + Math.random() * 4, rot: Math.random() * Math.PI, vrot: (Math.random() - 0.5) * 2,
            life: 1,
          });
        }
      }

      // 时停冰晶渲染：六角形冰晶，缓慢旋转下坠，时停结束后逐渐消散。
      for (const ice of this.iceCrystals) {
        if (moving && game.p.freeze > 0) {
          ice.x += ice.vx * dt; ice.y += ice.vy * dt; ice.rot += ice.vrot * dt;
        }
        // 时停结束后冰晶不再生成，存量冰晶在 0.5 秒内淡出消失。
        if (game.p.freeze <= 0) ice.life -= dt * 2;
        if (ice.y > P.H + 10 || ice.life <= 0) continue;
        c.save(); c.translate(ice.x, ice.y); c.rotate(ice.rot);
        c.globalAlpha = Math.max(0, Math.min(1, ice.life)) * 0.7;
        c.strokeStyle = '#a0e0f0'; c.lineWidth = 1.5;
        c.beginPath();
        for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; const x = Math.cos(a) * ice.size, y = Math.sin(a) * ice.size; if (i === 0) c.moveTo(x, y); else c.lineTo(x, y); }
        c.closePath(); c.stroke();
        c.fillStyle = 'rgba(160, 224, 240, 0.15)'; c.fill();
        c.globalAlpha = 1; c.restore();
      }
      this.iceCrystals = this.iceCrystals.filter(ice => ice.life > 0 && ice.y < P.H + 20);

      // ===== 敌机渲染：按类型分色 + 独立造型 + 发光核心 =====
      const enemyColors = { scout: '#9aa7ad', weaver: '#8a9ab0', tank: '#6a7a82', suicide: '#d44a3f' };
      const bossColor = { 1: '#c58873', 2: '#b5654a', 3: '#8a3b2e' };
      for (const e of game.enemies) {
        const isBoss = e.type === 'boss';
        const col = isBoss ? bossColor[e.bossPhase || 1] : (enemyColors[e.type] || '#91a7ac');
        const flash = e.flash > 0 || (isBoss && e.phaseFlash > 0);
        if (isBoss) this.drawBoss(c, e, col, flash);
        else this.drawEnemy(c, e, col, flash);
      }

      // ===== 玩家战机 + 副机 =====
      const p = game.p;
      if (p.inv <= 0 || Math.floor(clock * 12) % 2 === 0 || game.state !== 'playing') {
        this.plane(p.x, p.y, 0.85, '#e8edf2', false, false, false);
      }
      // 主战机引擎光焰
      if (game.state === 'playing') this.engineFlame(c, p.x, p.y + 22, 10, game.time, 4);

      // 副机：小号战机 + 短焰
      for (const w of p.wingmen) {
        this.plane(w.x, w.y, 0.5, '#8ec0d8', false, false, false);
        if (game.state === 'playing') this.engineFlame(c, w.x, w.y + 14, 5, game.time, 2.5);
      }

      // 护盾：旋转六边形能量罩 + 外层柔和光环，双层结构更有"能量场"的厚度感。
      if (p.shield > 0) {
        const rot = game.time * 1.5;
        // 外层光环：大半径、低透明度的渐变圆，模拟能量场扩散。
        const halo = c.createRadialGradient(p.x, p.y, 30, p.x, p.y, 50);
        halo.addColorStop(0, 'rgba(80, 220, 240, 0)');
        halo.addColorStop(0.7, `rgba(80, 220, 240, ${0.1 + Math.sin(game.time * 6) * 0.05})`);
        halo.addColorStop(1, 'rgba(80, 220, 240, 0)');
        c.fillStyle = halo; c.beginPath(); c.arc(p.x, p.y, 50, 0, Math.PI * 2); c.fill();
        // 内层六边形：旋转的能量罩轮廓。
        c.save(); c.translate(p.x, p.y); c.rotate(rot);
        c.strokeStyle = `rgba(80, 220, 240, ${0.4 + Math.sin(game.time * 8) * 0.2})`;
        c.shadowColor = '#50dcec'; c.shadowBlur = 10;
        c.lineWidth = 2; c.beginPath();
        for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; const x = Math.cos(a) * 35, y = Math.sin(a) * 35; if (i === 0) c.moveTo(x, y); else c.lineTo(x, y); }
        c.closePath(); c.stroke();
        c.shadowBlur = 0; c.restore();
      }

      // 擦弹闪光环：双层扩散环 + 渐变色 + 火花粒子，强化"擦出火花"的反馈。
      for (const g of this.grazes) {
        if (moving) g.life -= dt;
        const ratio = g.life / g.max;
        const radius = 18 + (1 - ratio) * 34;
        // 外环：大、淡、扩散，模拟冲击波。
        c.globalAlpha = Math.max(0, ratio) * 0.5;
        c.strokeStyle = `rgba(80, 220, 240, ${Math.max(0, ratio) * 0.6})`;
        c.lineWidth = 1.5;
        c.beginPath(); c.arc(g.x, g.y, radius + 6, 0, Math.PI * 2); c.stroke();
        // 内环：小、亮、实，模拟火花核心。
        c.globalAlpha = Math.max(0, ratio) * 0.9;
        c.shadowColor = '#50dcec'; c.shadowBlur = 10;
        c.strokeStyle = `rgba(200, 250, 255, ${Math.max(0, ratio)})`;
        c.lineWidth = 2.5;
        c.beginPath(); c.arc(g.x, g.y, radius, 0, Math.PI * 2); c.stroke();
        // 火花：沿环的 6 个方向画小光点，模拟擦弹溅射。
        if (ratio > 0.5) {
          c.fillStyle = `rgba(255, 255, 255, ${(ratio - 0.5) * 2})`;
          for (let i = 0; i < 6; i++) {
            const a = i * Math.PI / 3 + g.life * 2;
            const sx = g.x + Math.cos(a) * radius, sy = g.y + Math.sin(a) * radius;
            c.beginPath(); c.arc(sx, sy, 2.5, 0, Math.PI * 2); c.fill();
          }
        }
        c.shadowBlur = 0;
      }
      c.globalAlpha = 1; this.grazes = this.grazes.filter(g => g.life > 0);

      // ===== 道具：发光方块 + 字母 =====
      for (const item of game.items) {
        const pulse = 1 + Math.sin(item.age * 6) * 0.08;
        const bgColor = { power: '#f4c66a', shield: '#50dcec', heal: '#7ed09a', bomb: '#ed5040', time: '#7eb8e8' }[item.type] || '#f4c66a';
        const letter = { power: 'P', shield: 'S', heal: 'L', bomb: 'B', time: 'T' }[item.type] || '';
        c.save(); c.translate(item.x, item.y); c.scale(pulse, pulse);
        // 外层辉光
        c.shadowColor = bgColor; c.shadowBlur = 12;
        c.fillStyle = bgColor; c.fillRect(-14, -14, 28, 28);
        c.shadowBlur = 0;
        // 内层边框
        c.strokeStyle = 'rgba(255, 255, 255, 0.3)'; c.lineWidth = 1; c.strokeRect(-13, -13, 26, 26);
        // 字母
        c.fillStyle = '#071e32'; c.textAlign = 'center'; c.font = 'bold 22px sans-serif';
        c.fillText(letter, 0, 8);
        c.restore();
      }

      // ===== 粒子：多层碎片 + 光晕环 =====
      for (const particle of this.particles) {
        if (moving) { particle.life -= dt; particle.x += particle.vx * dt; particle.y += particle.vy * dt; particle.vx *= 0.96; particle.vy *= 0.96; }
        const ratio = Math.max(0, particle.life / particle.max);
        if (particle.ring) {
          // 光晕环：从中心向外扩散，半径增大、透明度降低。
          const r = (1 - ratio) * 40 + 5;
          c.globalAlpha = ratio * 0.6;
          c.strokeStyle = particle.color; c.lineWidth = 3 * ratio;
          c.beginPath(); c.arc(particle.x, particle.y, r, 0, Math.PI * 2); c.stroke();
        } else {
          c.globalAlpha = ratio;
          // 粒子辉光：半透明大方块叠加在碎片下方，替代 shadowBlur。
          // 爆炸时粒子数量多，shadowBlur 每帧开销大；半透明叠加视觉效果相近。
          const sz = particle.size || 4;
          c.fillStyle = particle.color;
          c.globalAlpha = ratio * 0.3;
          c.fillRect(particle.x - sz, particle.y - sz, sz * 2, sz * 2);
          c.globalAlpha = ratio;
          c.fillRect(particle.x - sz / 2, particle.y - sz / 2, sz, sz);
        }
      }
      c.globalAlpha = 1; this.particles = this.particles.filter(particle => particle.life > 0);

      // 得分飘字：向上浮动 + 淡出；倍率 ≥2x 金色加粗发光。
      for (const f of this.floaters) {
        if (moving) f.life -= dt;
        const ratio = f.life / f.max, y = f.y - (1 - ratio) * 48;
        c.globalAlpha = Math.max(0, ratio);
        const high = f.value >= 200;
        // 飘字辉光：倍率高时先画一层半透明金色底，替代 shadowBlur。
        if (high) { c.fillStyle = 'rgba(244, 198, 106, 0.25)'; c.fillText(`${f.value}`, f.x, y); }
        c.fillStyle = high ? '#f4c66a' : '#f3edda';
        c.font = `bold ${Math.round(16 + Math.log2(1 + f.value / 100))}px sans-serif`;
        c.textAlign = 'center';
        c.fillText(`${f.value}`, f.x, y);
        c.shadowBlur = 0;
      }
      c.globalAlpha = 1; this.floaters = this.floaters.filter(f => f.life > 0);

      // Boss 血量条：带辉光的分段条。
      if (game.boss) {
        c.fillStyle = 'rgba(7, 30, 50, 0.8)'; c.fillRect(35, 28, 410, 8);
        c.shadowColor = '#ed5040'; c.shadowBlur = 6;
        c.fillStyle = '#ed5040'; c.fillRect(35, 28, 410 * Math.max(0, game.boss.hp / game.boss.maxHp), 8);
        c.shadowBlur = 0;
        for (const ratio of [0.66, 0.33]) c.fillStyle = '#030a14', c.fillRect(35 + 410 * ratio, 28, 2, 8);
        c.fillStyle = '#f3edda'; c.font = '11px sans-serif'; c.fillText('空中堡垒', 35, 21);
      }

      // Boss 出场预警：顶部红色闪烁警报条。
      if (this.bossWarn > 0) {
        if (moving) this.bossWarn -= dt;
        const ratio = Math.max(0, this.bossWarn / 3);
        const blink = Math.sin(this.bossWarn * 12) > 0;
        c.globalAlpha = ratio * (blink ? 0.9 : 0.25);
        c.shadowColor = '#ed3b3b'; c.shadowBlur = 10;
        c.fillStyle = '#ed3b3b'; c.fillRect(0, 0, P.W, 6);
        c.shadowBlur = 0;
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
        if (mul >= 2) { c.shadowColor = '#f4c66a'; c.shadowBlur = 14; }
        c.fillStyle = mul >= 2 ? '#f4c66a' : '#f3edda';
        c.font = 'bold 24px sans-serif'; c.fillText(`×${mul}`, 0, -2);
        c.shadowBlur = 0; c.fillStyle = '#c0d0d7'; c.font = 'bold 13px sans-serif'; c.fillText(`${game.combo} COMBO`, 0, 15);
        const ratio = game.comboTimer / 2.5, barW = 56, barH = 3;
        c.fillStyle = '#071e32'; c.fillRect(-barW / 2, 24, barW, barH);
        c.fillStyle = ratio > 0.4 ? '#f4c66a' : '#ed7458'; c.fillRect(-barW / 2, 24, barW * ratio, barH);
        c.restore();
      }

      // 时停结束闪光
      if (this.freezeFlash > 0) {
        c.fillStyle = `rgba(255, 255, 255, ${this.freezeFlash / 0.25 * 0.25})`;
        c.fillRect(0, 0, P.W, P.H);
      }
      // Boss 阶段切换波纹：从 Boss 位置向外扩散的双环白波，0.6 秒内淡出。
      // 比单纯震屏更有"形态变化"的仪式感，玩家能直观感受到阶段切换。
      if (this.bossWave > 0 && this.reduced === false) {
        if (moving) this.bossWave -= dt;
        const ratio = Math.max(0, this.bossWave / 0.6);
        // 外环：大、淡
        const r1 = (1 - ratio) * 320 + 20;
        c.globalAlpha = ratio * 0.4;
        c.strokeStyle = '#ffe8d0'; c.lineWidth = 4 * ratio + 1;
        c.beginPath(); c.arc(this.bossWaveX, this.bossWaveY, r1, 0, Math.PI * 2); c.stroke();
        // 内环：小、亮
        const r2 = (1 - ratio) * 180 + 10;
        c.globalAlpha = ratio * 0.7;
        c.strokeStyle = '#ffffff'; c.lineWidth = 3 * ratio + 1;
        c.beginPath(); c.arc(this.bossWaveX, this.bossWaveY, r2, 0, Math.PI * 2); c.stroke();
        c.globalAlpha = 1;
      }
      // 受伤红屏：全屏红色叠加，0.3 秒内淡出，强化"被打"的体感。
      if (this.hurtFlash > 0) {
        if (moving) this.hurtFlash -= dt;
        c.fillStyle = `rgba(237, 40, 30, ${Math.max(0, this.hurtFlash) / 0.3 * 0.35})`;
        c.fillRect(0, 0, P.W, P.H);
      }
      // 炸弹白光：全屏白色闪光，0.4 秒内淡出，强化"清屏"的爽感。
      if (this.bombFlash > 0) {
        if (moving) this.bombFlash -= dt;
        c.fillStyle = `rgba(255, 255, 255, ${Math.max(0, this.bombFlash) / 0.4 * 0.5})`;
        c.fillRect(0, 0, P.W, P.H);
      }
      // 连击边缘金色色调：倍率越高，战场边缘的金色光晕越强，
      // 给玩家"手感火热"的体感，比右上角数字更直观。
      // 用径向渐变实现：中心透明、边缘金色，透明度由倍率决定。
      if (game.combo > 0 && game.comboMul >= 2 && !this.reduced) {
        const intensity = Math.min((game.comboMul - 1) / 3, 0.35);
        const comboTint = c.createRadialGradient(P.W / 2, P.H / 2, P.H * 0.25, P.W / 2, P.H / 2, P.H * 0.7);
        comboTint.addColorStop(0, 'rgba(244, 198, 106, 0)');
        comboTint.addColorStop(1, `rgba(244, 198, 106, ${intensity})`);
        c.fillStyle = comboTint; c.fillRect(0, 0, P.W, P.H);
      }
      // 暗角（vignette）：战场边缘叠加径向渐变，中心透明、边缘渐暗，
      // 模拟街机屏幕的物理暗角，把注意力收拢到中央战场，提升沉浸感。
      const vignette = c.createRadialGradient(P.W / 2, P.H / 2, P.H * 0.3, P.W / 2, P.H / 2, P.H * 0.72);
      vignette.addColorStop(0, 'rgba(0, 0, 0, 0)');
      vignette.addColorStop(1, 'rgba(0, 0, 0, 0.45)');
      c.fillStyle = vignette; c.fillRect(0, 0, P.W, P.H);
      c.restore();
    }
  }
  P.Effects = Effects;
})(globalThis);
