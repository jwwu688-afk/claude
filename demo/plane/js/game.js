/* ===== 战斗核心：固定逻辑坐标与秒制时间，不依赖 DOM。 =====
 * 【2026-11-10】输入交给 main，画面交给 effects；测试可直接推进时间。
 * 死亡标记在本帧立即生效，帧末统一回收，避免多发子弹重复计分。
 * 【2026-11-11】连击：comboTimer 从 2.5 秒倒计时，击杀续满；
 * 倍率 = 1 + floor(combo / 10)，分数 = baseScore × 倍率；受伤与超时清零。
 * 擦弹：敌弹与玩家距离在 (hitR, hitR+GRAZE] 内触发，同一发只擦一次，
 * 加 20 分并发 graze 事件；与连击独立，鼓励贴弹走位。
 */
(function (root) {
  'use strict';
  const P = root.Plane;
  class Game {
    /** rng 可注入确定性随机源；emit 接收音效和爆炸事件。 */
    constructor(rng = Math.random, emit = () => {}) { this.rng = rng; this.emit = emit; this.reset(); this.state = 'ready'; }
    /** 每次开局重建所有数组，避免上局弹幕和强化残留。连击也清零。 */
    reset() {
      this.p = P.player(); this.enemies = []; this.shots = []; this.bullets = []; this.items = [];
      // nextBoss=40：开局首个 Boss 间隔；击杀后按等级递减，见 kill()。
      this.score = 0; this.time = 0; this.level = 1; this.spawn = 0.7; this.nextBoss = 40; this.boss = null; this.state = 'playing';
      this.combo = 0; this.comboTimer = 0; this.bossWarned = false;
      // 【2026-11-13】结算统计：击杀数与最高连击，供结算画面展示本局表现。
      this.kills = 0; this.bestCombo = 0;
    }
    /** 连击倍率：0-9 = 1x，10-19 = 2x，20-29 = 3x… */
    get comboMul() { return 1 + Math.floor(this.combo / 10); }
    /** 连击结束（超时或受伤）时触发；仅在有连击时清零。 */
    _resetCombo() {
      if (this.combo === 0) return;
      this.combo = 0; this.comboTimer = 0;
    }
    /** 只在游玩与暂停之间切换，结算不会被误恢复。 */
    pause() { if (this.state === 'playing') this.state = 'paused'; else if (this.state === 'paused') this.state = 'playing'; }
    /** 炸弹不伤玩家；复制目标列表，避免掉落/击杀修改迭代。 */
    bomb() {
      if (this.state !== 'playing' || !this.p.bombs) return false;
      this.p.bombs--; this.bullets = []; this.p.inv = Math.max(this.p.inv, 1);
      for (const e of [...this.enemies]) { e.hp -= 45; e.flash = 0.2; if (e.hp <= 0) this.kill(e); }
      this.emit('bomb', this.p); return true;
    }
    /** dead 是幂等锁；Boss 保证补给，其余按概率掉落。击杀计入连击。 */
    kill(e) {
      if (e.dead) return;
      e.dead = true;
      // 连击先递增再算分：1 杀=1x，10 杀=2x，但首杀不能先算 1x 再晋级。
      this.combo++; this.comboTimer = 2.5;
      // 【2026-11-13】统计：击杀数累加，最高连击在每次击杀时更新。
      this.kills++; if (this.combo > this.bestCombo) this.bestCombo = this.combo;
      const gained = e.score * this.comboMul;
      this.score += gained;
      this.emit(e.type === 'boss' ? 'bomb' : 'explosion', e);
      this.emit('score', { x: e.x, y: e.y, value: gained });
      if (this.combo > 0 && this.combo % 10 === 0) this.emit('combo', { combo: this.combo });
      // Boss 间隔随等级递减：前期 40 秒、后期最低 25 秒，难度递进。
      if (e === this.boss) { this.boss = null; this.nextBoss = this.time + Math.max(25, 40 - this.level * 1.5); }
      if (e.type === 'boss' || this.rng() < 0.2) {
        const kinds = ['power', 'shield', 'heal', 'bomb', 'time'];
        this.items.push(P.pickup(e.x, e.y, kinds[Math.floor(this.rng() * kinds.length)]));
      }
    }
    /** 护盾先吸收一次伤害；无敌期不消耗生命或护盾。受伤清零连击。 */
    damage() {
      const p = this.p;
      if (p.inv > 0) return;
      this._resetCombo();
      if (p.shield > 0) p.shield = 0; else p.lives--;
      p.inv = 2; this.emit('hurt', p);
      if (p.lives <= 0) { this.state = 'over'; this.emit('over', p); }
    }
    /** 补给有上限，回血不会超过初始三条命，火力持续十二秒。
     * 先做 dead 幂等检查：同帧内补给可能被碰撞与遍历各收集一次，
     * 不检查会导致火力/护盾/时停被重复叠加。 */
    collect(item) {
      if (item.dead) return;
      const p = this.p; item.dead = true;
      if (item.type === 'power') { p.weapon = Math.min(3, p.weapon + 1); p.power = 12; }
      if (item.type === 'shield') p.shield = 12;
      if (item.type === 'heal') p.lives = Math.min(3, p.lives + 1);
      if (item.type === 'bomb') p.bombs = Math.min(5, p.bombs + 1);
      if (item.type === 'time') p.freeze = 4;
      this.emit('pickup', item);
      // 【2026-11-13】时停激活音效：pickup 事件负责粒子，freeze 事件只负责音效，
      // 职责分离避免给所有补给都加相同的音效；正弦上扬音与视觉的青蓝色调呼应。
      if (item.type === 'time') this.emit('freeze', item);
    }
    /** dt 限制到 50ms，切回标签页不会瞬移；input 为方向或触摸目标。 */
    update(dt, input = {}) {
      if (this.state !== 'playing') return;
      dt = P.clamp(Number.isFinite(dt) ? dt : 0, 0, 0.05);
      this.time += dt; this.level = 1 + Math.floor(this.time / 20);
      // 连击倒计时：暂停时由上方 state 检查拦截，不会偷偷清零。
      if (this.comboTimer > 0) { this.comboTimer -= dt; if (this.comboTimer <= 0) this._resetCombo(); }
      const p = this.p;
      for (const key of ['inv', 'shield', 'power', 'freeze']) p[key] = Math.max(0, p[key] - dt);
      if (!p.power) p.weapon = 1;
      let dx = input.x || 0, dy = input.y || 0;
      if (input.target) {
        // 触摸：直接追踪目标点，不经过速度分量——手指位置本身就是权威指令。
        dx = input.target.x - p.x; dy = input.target.y - p.y;
        const length = Math.hypot(dx, dy);
        const step = Math.min(length, 350 * dt);
        if (length) { p.x += dx / length * step; p.y += dy / length * step; }
      } else {
        // 【2026-11-13】键盘平滑：vx/vy 向目标速度指数平滑，方向切换时有惯性，
        // 手感更跟手；对角线方向单位化后两轴独立平滑，总速度不变（不加速）。
        // 平滑系数 12/秒：约 0.25 秒达到目标速度，不会太迟钝也不会太硬。
        const length = Math.hypot(dx, dy);
        if (length) {
          const targetVx = dx / length * 300, targetVy = dy / length * 300;
          const smooth = Math.min(1, 12 * dt);
          p.vx += (targetVx - p.vx) * smooth;
          p.vy += (targetVy - p.vy) * smooth;
        } else {
          // 松开按键：速度衰减到零，避免惯性漂移导致"松开后还在滑"。
          const decay = Math.min(1, 8 * dt);
          p.vx += (0 - p.vx) * decay;
          p.vy += (0 - p.vy) * decay;
        }
        p.x += p.vx * dt; p.y += p.vy * dt;
      }
      p.x = P.clamp(p.x, 22, P.W - 22); p.y = P.clamp(p.y, 30, P.H - 26);
      // 撞到边界时把该轴速度归零，否则 clamp 每帧都在拉回，vx 却仍指向墙外，
      // 松键后速度衰减会从"负值"开始，产生一段朝墙内的反向漂移。
      if (p.x <= 22 && p.vx < 0) p.vx = 0; if (p.x >= P.W - 22 && p.vx > 0) p.vx = 0;
      if (p.y <= 30 && p.vy < 0) p.vy = 0; if (p.y >= P.H - 26 && p.vy > 0) p.vy = 0;
      p.fire -= dt;
      if (p.fire <= 0) {
        p.fire = 0.15;
        for (let i = 0; i < p.weapon; i++) this.shots.push(P.bullet(p.x + (i - (p.weapon - 1) / 2) * 15, p.y - 26, (i - (p.weapon - 1) / 2) * 28, -590));
        this.emit('shot', p);
      }
      // 【2026-11-12】Boss 出场前 3 秒发 bosswarn 事件，渲染层据此画顶部红色警报条。
      // bossWarned 防止每次帧都触发，Boss 出现或重置时清零。
      if (!this.boss && !this.bossWarned && this.time >= this.nextBoss - 3) {
        this.bossWarned = true; this.emit('bosswarn', { time: this.nextBoss - this.time });
      }
      if (!this.boss && this.time >= this.nextBoss) {
        this.boss = P.enemy('boss', this.level, this.rng); this.enemies.push(this.boss); this.bossWarned = false;
      }
      this.spawn -= dt;
      if (!this.boss && this.spawn <= 0) {
        this.spawn = Math.max(0.22, 1 - this.level * 0.07);
        const type = this.rng() < 0.2 ? 'suicide' : ['scout', 'weaver', 'tank'][Math.floor(this.rng() * 3)];
        this.enemies.push(P.enemy(type, this.level, this.rng));
      }
      for (const e of this.enemies) {
        if (e.dead) continue;
        e.age += dt; e.flash = Math.max(0, e.flash - dt); e.phaseFlash = Math.max(0, e.phaseFlash - dt);
        if (e.type === 'boss' && e.y >= 105) { e.x = 240 + Math.sin(e.age * 0.7) * 135; }
        else e.y += e.speed * dt;
        if (e.type === 'weaver') e.x = P.clamp(e.x + Math.sin(e.age * 3 + e.phase) * 100 * dt, 25, 455);
        // 【2026-11-12】自爆机横向追踪玩家 x 坐标，迫使玩家主动走位躲避。
        if (e.type === 'suicide') e.x = P.clamp(e.x + (p.x - e.x) * 1.8 * dt, 20, P.W - 20);
        // 【2026-11-12】Boss 阶段按血量比例判定：>66% 一阶段，>33% 二阶段，否则三阶段。
        // 阶段变化时触发 phaseFlash 闪烁并发 bossphase 事件，渲染层据此变色。
        if (e.type === 'boss') {
          const ratio = e.hp / e.maxHp;
          const np = ratio > 0.66 ? 1 : (ratio > 0.33 ? 2 : 3);
          if (np !== e.bossPhase) { e.bossPhase = np; e.phaseFlash = 0.6; e.bossAlt = 0; this.emit('bossphase', { phase: np }); }
        }
        e.fire -= dt;
        // 【2026-11-12】自爆机接近玩家时发射环形弹幕后自毁；不给分、不计连击，
        // 与击落（走 kill()）区分——这是"陷阱"而非"奖励"。
        if (e.type === 'suicide' && !e.dead && Math.hypot(p.x - e.x, p.y - e.y) < 80) {
          for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 175, Math.sin(a) * 175, true)); }
          e.dead = true; this.emit('explosion', e);
        }
        if (e.type !== 'scout' && e.y > 0 && e.fire <= 0 && e.type !== 'suicide') {
          if (e.type === 'boss') {
            // 三阶段间隔递减：0.65 → 0.8（环形弹幕节奏慢但覆盖广）→ 0.5（狂暴连发）。
            e.fire = e.bossPhase === 1 ? 0.65 : (e.bossPhase === 2 ? 0.8 : 0.5);
            const angle = Math.atan2(p.y - e.y, p.x - e.x);
            if (e.bossPhase === 1) {
              // 阶段一：扇形 5 发朝玩家，经典单点压制。
              for (let i = 0; i < 5; i++) { const a = angle + (i - 2) * 0.23; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 175, Math.sin(a) * 175, true)); }
            } else if (e.bossPhase === 2) {
              // 阶段二：环形 8 发全方向，封走位逼擦弹。
              for (let i = 0; i < 8; i++) { const a = i * Math.PI / 4; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 175, Math.sin(a) * 175, true)); }
            } else {
              // 阶段三：扇形 5 发与追踪 3 发交替发射，由 bossAlt 计数器切换。
              // 交替而非齐射——齐射一次 8 发会瞬间封死走位，远超"狂暴"的设计意图；
              // 交替则玩家有 0.5 秒窗口判断本轮是弹幕还是追踪弹，留出应对空间。
              if (e.bossAlt === 0) {
                for (let i = 0; i < 5; i++) { const a = angle + (i - 2) * 0.23; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 175, Math.sin(a) * 175, true)); }
                e.bossAlt = 1;
              } else {
                for (let i = 0; i < 3; i++) { const a = angle + (i - 1) * 0.5; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 160, Math.sin(a) * 160, true, true)); }
                e.bossAlt = 0;
              }
            }
            this.emit('shot', e);
          } else {
            e.fire = 1.7;
            const angle = Math.atan2(p.y - e.y, p.x - e.x);
            this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(angle) * 175, Math.sin(angle) * 175, true));
          }
        }
        if (P.hit(p, e)) { this.damage(); if (e.type !== 'boss') e.dead = true; }
        if (e.y > P.H + 80) e.dead = true;
      }
      for (const b of [...this.shots, ...this.bullets]) {
        // 【2026-11-13】时停补给只冻敌弹（hostile），不冻玩家自己的 shots——
        // 否则玩家趁机跑到安全位，时停就变成纯福利，失去"用 4 秒换走位窗口"的战术意义。
        if (!(b.hostile && p.freeze > 0)) { b.x += b.vx * dt; b.y += b.vy * dt; }
        // 【2026-11-12】追踪弹（track）每帧朝玩家微调方向，保持速度大小不变。
        // 只做角度转向，不做加速——避免追踪弹比常规弹快导致难度失控。
        // 时停期间同样冻结转向：否则会看到追踪弹在静止弹幕里独自转向，视觉上很怪。
        if (b.track && !b.dead && p.freeze <= 0) {
          const ang = Math.atan2(p.y - b.y, p.x - b.x);
          const cur = Math.atan2(b.vy, b.vx);
          let diff = ang - cur;
          if (diff > Math.PI) diff -= Math.PI * 2; else if (diff < -Math.PI) diff += Math.PI * 2;
          const turn = P.clamp(diff, -3.2 * dt, 3.2 * dt);
          const speed = Math.hypot(b.vx, b.vy);
          b.vx = Math.cos(cur + turn) * speed; b.vy = Math.sin(cur + turn) * speed;
        }
        if (b.y < -30 || b.y > P.H + 30 || b.x < -30 || b.x > P.W + 30) b.dead = true;
        if (b.hostile) {
          // 擦弹：距离在 (hitR, hitR+GRAZE] 内且该弹未擦过，加 20 分并发 graze 事件。
          // 命中判定优先：先判 hit，再判擦弹，避免同帧既擦又命中。
          if (P.hit(b, p)) { b.dead = true; this.damage(); }
          else if (!b.grazed) {
            const d = Math.hypot(b.x - p.x, b.y - p.y), hitR = b.r + p.r;
            if (d > hitR && d <= hitR + P.GRAZE) {
              b.grazed = true; this.score += 20;
              this.emit('graze', { x: p.x, y: p.y });
            }
          }
        }
        else for (const e of this.enemies) if (P.hit(b, e)) { b.dead = true; e.hp--; e.flash = 0.07; if (e.hp <= 0) this.kill(e); break; }
      }
      for (const item of this.items) { item.age += dt; item.y += 85 * dt; if (P.hit(item, p)) this.collect(item); if (item.y > P.H + 20) item.dead = true; }
      for (const key of ['enemies', 'shots', 'bullets', 'items']) this[key] = this[key].filter(e => !e.dead);
    }
  }
  P.Game = Game;
  if (typeof module !== 'undefined' && module.exports) module.exports = Game;
})(globalThis);
