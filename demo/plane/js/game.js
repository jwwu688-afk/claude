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
      // 【2026-11-14】Boss 击毁演出：慢动作计时与死亡序列状态，重置时清零。
      this.slowmo = 0; this.bossDying = null;
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
    /** 【2026-11-14】按波次选择敌机类型，形成递进难度曲线。
     * 波次 1-3：侦察机为主，少量织网机——让玩家熟悉基础走位。
     * 波次 4-6：增加织网机和重装机——弹幕密度上升，考验走位。
     * 波次 7-9：引入自爆机——压力增大，织网机比例提高。
     * 波次 10+：混合所有类型，自爆机比例最高——节奏最快。 */
    pickEnemyType(level) {
      if (level <= 3) return this.rng() < 0.7 ? 'scout' : 'weaver';
      if (level <= 6) {
        const r = this.rng();
        if (r < 0.35) return 'scout';
        if (r < 0.7) return 'weaver';
        return 'tank';
      }
      if (level <= 9) {
        const r = this.rng();
        if (r < 0.3) return 'scout';
        if (r < 0.6) return 'weaver';
        if (r < 0.8) return 'tank';
        return 'suicide';
      }
      const r = this.rng();
      if (r < 0.2) return 'scout';
      if (r < 0.45) return 'weaver';
      if (r < 0.7) return 'tank';
      return 'suicide';
    }
    /** 炸弹不伤玩家；复制目标列表，避免掉落/击杀修改迭代。 */
    bomb() {
      if (this.state !== 'playing' || !this.p.bombs) return false;
      this.p.bombs--; this.bullets = []; this.p.inv = Math.max(this.p.inv, 1);
      for (const e of [...this.enemies]) { e.hp -= 45; e.flash = 0.2; if (e.hp <= 0) this.kill(e); }
      this.emit('bomb', this.p); return true;
    }
    /** dead 是幂等锁；Boss 保证补给，其余按概率掉落。击杀计入连击。
     * 【2026-11-14】Boss 击毁演出：Boss 死亡时不立即清除，而是进入 bossDying
     * 序列——慢动作 2 秒 + 多段爆炸，结束后才真正清除并刷新下一只。 */
    kill(e) {
      if (e.dead) return;
      e.dead = true;
      // 连击先递增再算分：1 杀=1x，10 杀=2x，但首杀不能先算 1x 再晋级。
      this.combo++; this.comboTimer = 2.5;
      // 【2026-11-13】统计：击杀数累加，最高连击在每次击杀时更新。
      this.kills++; if (this.combo > this.bestCombo) this.bestCombo = this.combo;
      const gained = e.score * this.comboMul;
      this.score += gained;
      if (e.type === 'boss') {
        // Boss 死亡序列：进入慢动作，保留实体用于播放爆炸，发 bossdeath 事件。
        // score 和 combo 事件仍然要发——玩家需要看到得分飘字和连击提示，
        // 只是爆炸演出和 Boss 清除延迟到序列结束后。
        this.emit('score', { x: e.x, y: e.y, value: gained });
        if (this.combo > 0 && this.combo % 10 === 0) this.emit('combo', { combo: this.combo });
        this.bossDying = { x: e.x, y: e.y, timer: 2.0, explosionTimer: 0 };
        this.slowmo = 2.0;
        this.emit('bossdeath', { x: e.x, y: e.y });
        // Boss 死后立即从 enemies 中移除，但位置信息已存入 bossDying 供演出使用。
        this.boss = null;
        // Boss 必掉补给，保证玩家不会空手进入下一轮。
        this.items.push(P.pickup(e.x, e.y, 'power'));
        return;
      }
      this.emit('explosion', e);
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
     * 不检查会导致火力/护盾/时停被重复叠加。
     * 【2026-11-14】武器系统：拾取 power 时循环切换武器类型（1散射→2激光→3导弹→1），
     * 三种武器风格截然不同，给玩家选择与策略空间。 */
    collect(item) {
      if (item.dead) return;
      const p = this.p; item.dead = true;
      if (item.type === 'power') { p.weapon = Math.min(3, p.weapon + 1); p.power = 12; p.weaponType = (p.weaponType % 3) + 1; }
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
      // 【2026-11-14】Boss 击毁慢动作：bossDying 激活时 dt 缩减到 25%，
      // 让整个战场进入"慢镜头"，配合多段爆炸和全屏白光，强化击毁 Boss 的仪式感。
      if (this.bossDying) {
        const slowDt = dt * 0.25;
        this.bossDying.timer -= dt;
        // 多段爆炸：每 0.4 秒在 Boss 残骸附近随机位置炸一次，模拟解体过程。
        this.bossDying.explosionTimer -= dt;
        if (this.bossDying.explosionTimer <= 0) {
          this.bossDying.explosionTimer = 0.4;
          this.emit('explosion', { x: this.bossDying.x + (Math.random() - 0.5) * 80, y: this.bossDying.y + (Math.random() - 0.5) * 60 });
        }
        // 序列结束：清除慢动作状态，安排下一只 Boss。
        if (this.bossDying.timer <= 0) {
          this.bossDying = null;
          this.slowmo = 0;
          this.nextBoss = this.time + Math.max(25, 40 - this.level * 1.5);
        }
        // 慢动作期间仍然推进游戏逻辑，但用缩减后的 dt。
        dt = slowDt;
      }
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
        // 【2026-11-14】武器系统：三种武器发射逻辑，用 weaponType 区分。
        // 1 散射：多发扇形子弹，覆盖面广，适合清小怪；
        // 2 激光：单发高伤穿透弹，穿过多个敌机，适合打 Boss 和密集阵列；
        // 3 导弹：追踪弹，自动锁定最近敌机，适合精准打击。
        if (p.weaponType === 1) {
          p.fire = 0.15;
          for (let i = 0; i < p.weapon; i++) this.shots.push(P.bullet(p.x + (i - (p.weapon - 1) / 2) * 15, p.y - 26, (i - (p.weapon - 1) / 2) * 28, -590, false, false, 'spread'));
        } else if (p.weaponType === 2) {
          p.fire = 0.22;
          // 激光：每发带 pierce，可穿透 3 个敌机；伤害在碰撞处理中按 pierce 扣减。
          for (let i = 0; i < p.weapon; i++) this.shots.push(P.bullet(p.x + (i - (p.weapon - 1) / 2) * 12, p.y - 26, (i - (p.weapon - 1) / 2) * 15, -680, false, false, 'laser', 3));
        } else {
          p.fire = 0.35;
          // 导弹：追踪最近敌机，发射时先朝正上方，之后每帧在 update 中转向。
          for (let i = 0; i < Math.max(1, p.weapon - 1); i++) this.shots.push(P.bullet(p.x + (i - (p.weapon - 2) / 2) * 20, p.y - 26, (i - (p.weapon - 2) / 2) * 40, -480, false, true, 'missile'));
        }
        this.emit('shot', p);
      }
      // 【2026-11-14】副机系统：两个副机贴身跟随主战机，独立计时开火。
      // 副机位置由主战机位置 + 固定偏移计算，玩家移动时副机自动跟随，不需要独立输入。
      // 副机开火频率比主炮快一倍（0.075s vs 0.15s），与主炮错开半拍，
      // 形成"主炮三发 + 副机双发"的更密集火力网，是雷霆战机的核心体验。
      // 副机受时停影响：freeze 期间敌弹冻结，但副机照常开火（与主炮一致）。
      for (const w of p.wingmen) {
        const ox = w === p.wingmen[0] ? -18 : 18;
        w.x = P.clamp(p.x + ox, 20, P.W - 20);
        w.y = p.y + 12;
        w.fire -= dt;
        if (w.fire <= 0) {
          w.fire = 0.075;
          this.shots.push(P.bullet(w.x, w.y - 18, 0, -590));
        }
      }
      // 【2026-11-12】Boss 出场前 3 秒发 bosswarn 事件，渲染层据此画顶部红色警报条。
      // bossWarned 防止每次帧都触发，Boss 出现或重置时清零。
      if (!this.boss && !this.bossWarned && this.time >= this.nextBoss - 3) {
        this.bossWarned = true; this.emit('bosswarn', { time: this.nextBoss - this.time });
      }
      if (!this.boss && !this.bossDying && this.time >= this.nextBoss) {
        this.boss = P.enemy('boss', this.level, this.rng); this.enemies.push(this.boss); this.bossWarned = false;
      }
      this.spawn -= dt;
      if (!this.boss && this.spawn <= 0) {
        // 【2026-11-14】难度曲线：按波次切换敌机组合，节奏递增。
        // 波次 1-3：侦察机为主，少量织网机，让玩家熟悉基础走位。
        // 波次 4-6：增加织网机和重装机，弹幕密度上升。
        // 波次 7-9：引入自爆机，压力增大，织网机比例提高。
        // 波次 10+：混合所有类型，自爆机比例最高，节奏最快。
        // 这样比固定随机更有节奏感，玩家能感知难度递增。
        this.spawn = Math.max(0.22, 1 - this.level * 0.07);
        const type = this.pickEnemyType(this.level);
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
              // 【2026-11-14】阶段三：螺旋弹幕。bossSpiral 是持续累加的旋转角，
              // 每轮发射时以当前角度为基准散开 4 发，下一轮整体旋转，
              // 形成不断旋转的螺旋弹幕——比"扇形+追踪交替"更难预判走位。
              // 旋转步长 0.42 弧度：约 7.5 轮转完一圈，节奏紧凑但留出穿插缝隙。
              for (let i = 0; i < 4; i++) {
                const a = e.bossSpiral + (i - 1.5) * 0.2;
                this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 170, Math.sin(a) * 170, true));
              }
              e.bossSpiral += 0.42;
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
        // 【2026-11-14】追踪逻辑分两路：
        // Boss 阶段三的 track 弹朝玩家转向；玩家导弹（shotType=missile）朝最近敌机转向。
        // 两者共享转向框架，但目标不同——导弹追敌、Boss 弹追玩家。
        if (b.track && !b.dead && p.freeze <= 0) {
          const ang = Math.atan2(p.y - b.y, p.x - b.x);
          const cur = Math.atan2(b.vy, b.vx);
          let diff = ang - cur;
          if (diff > Math.PI) diff -= Math.PI * 2; else if (diff < -Math.PI) diff += Math.PI * 2;
          const turn = P.clamp(diff, -3.2 * dt, 3.2 * dt);
          const speed = Math.hypot(b.vx, b.vy);
          b.vx = Math.cos(cur + turn) * speed; b.vy = Math.sin(cur + turn) * speed;
        }
        // 导弹追踪最近敌机：每帧找距离最近的存活敌机，朝它转向。
        if (b.shotType === 'missile' && !b.dead && p.freeze <= 0) {
          let nearest = null, bestD = Infinity;
          for (const e of this.enemies) { if (e.dead) continue; const d = Math.hypot(b.x - e.x, b.y - e.y); if (d < bestD) { bestD = d; nearest = e; } }
          if (nearest) {
            const ang = Math.atan2(nearest.y - b.y, nearest.x - b.x);
            const cur = Math.atan2(b.vy, b.vx);
            let diff = ang - cur;
            if (diff > Math.PI) diff -= Math.PI * 2; else if (diff < -Math.PI) diff += Math.PI * 2;
            const turn = P.clamp(diff, -4.0 * dt, 4.0 * dt);
            const speed = Math.hypot(b.vx, b.vy);
            b.vx = Math.cos(cur + turn) * speed; b.vy = Math.sin(cur + turn) * speed;
          }
        }
        if (b.y < -30 || b.y > P.H + 30 || b.x < -30 || b.x > P.W + 30) b.dead = true;
        // 【2026-11-14】激光穿透：shotType=laser 且 pierce>0 时不立即销毁，
        // 记录已击中的敌机防止重复伤害，每穿透一个消耗一次 pierce。
        // 导弹（shotType=missile）命中后爆炸销毁；散射弹保持原逻辑一击即销毁。
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
        else {
          for (const e of this.enemies) {
            if (b.dead) break;
            if (P.hit(b, e)) {
              // 激光：检查是否已击中过该敌机，没击中过则造成伤害并消耗 pierce。
              if (b.shotType === 'laser') {
                if (!b.hits.includes(e)) { b.hits.push(e); e.hp--; e.flash = 0.07; if (e.hp <= 0) this.kill(e); b.pierce--; }
                if (b.pierce <= 0) b.dead = true;
              } else {
                b.dead = true; e.hp--; e.flash = 0.07; if (e.hp <= 0) this.kill(e);
              }
            }
          }
        }
      }
      for (const item of this.items) { item.age += dt; item.y += 85 * dt; if (P.hit(item, p)) this.collect(item); if (item.y > P.H + 20) item.dead = true; }
      for (const key of ['enemies', 'shots', 'bullets', 'items']) this[key] = this[key].filter(e => !e.dead);
    }
  }
  P.Game = Game;
  if (typeof module !== 'undefined' && module.exports) module.exports = Game;
})(globalThis);
