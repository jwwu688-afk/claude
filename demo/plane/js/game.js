/* ===== 战斗核心：固定逻辑坐标与秒制时间，不依赖 DOM。 =====
 * 【2026-11-10】输入交给 main，画面交给 effects；测试可直接推进时间。
 * 死亡标记在本帧立即生效，帧末统一回收，避免多发子弹重复计分。
 */
(function (root) {
  'use strict';
  const P = root.Plane;
  class Game {
    /** rng 可注入确定性随机源；emit 接收音效和爆炸事件。 */
    constructor(rng = Math.random, emit = () => {}) { this.rng = rng; this.emit = emit; this.reset(); this.state = 'ready'; }
    /** 每次开局重建所有数组，避免上局弹幕和强化残留。 */
    reset() {
      this.p = P.player(); this.enemies = []; this.shots = []; this.bullets = []; this.items = [];
      this.score = 0; this.time = 0; this.level = 1; this.spawn = 0.7; this.nextBoss = 35; this.boss = null; this.state = 'playing';
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
    /** dead 是幂等锁；Boss 保证补给，其余按概率掉落。 */
    kill(e) {
      if (e.dead) return;
      e.dead = true; this.score += e.score; this.emit('explosion', e);
      if (e === this.boss) { this.boss = null; this.nextBoss = this.time + 35; }
      if (e.type === 'boss' || this.rng() < 0.2) {
        const kinds = ['power', 'shield', 'heal', 'bomb'];
        this.items.push(P.pickup(e.x, e.y, kinds[Math.floor(this.rng() * kinds.length)]));
      }
    }
    /** 护盾先吸收一次伤害；无敌期不消耗生命或护盾。 */
    damage() {
      const p = this.p;
      if (p.inv > 0) return;
      if (p.shield > 0) p.shield = 0; else p.lives--;
      p.inv = 2; this.emit('hurt', p);
      if (p.lives <= 0) { this.state = 'over'; this.emit('over', p); }
    }
    /** 补给有上限，回血不会超过初始三条命，火力持续十二秒。 */
    collect(item) {
      const p = this.p; item.dead = true;
      if (item.type === 'power') { p.weapon = Math.min(3, p.weapon + 1); p.power = 12; }
      if (item.type === 'shield') p.shield = 12;
      if (item.type === 'heal') p.lives = Math.min(3, p.lives + 1);
      if (item.type === 'bomb') p.bombs = Math.min(5, p.bombs + 1);
      this.emit('pickup', item);
    }
    /** dt 限制到 50ms，切回标签页不会瞬移；input 为方向或触摸目标。 */
    update(dt, input = {}) {
      if (this.state !== 'playing') return;
      dt = P.clamp(Number.isFinite(dt) ? dt : 0, 0, 0.05);
      this.time += dt; this.level = 1 + Math.floor(this.time / 20);
      const p = this.p;
      for (const key of ['inv', 'shield', 'power']) p[key] = Math.max(0, p[key] - dt);
      if (!p.power) p.weapon = 1;
      let dx = input.x || 0, dy = input.y || 0;
      if (input.target) { dx = input.target.x - p.x; dy = input.target.y - p.y; }
      const length = Math.hypot(dx, dy);
      const step = input.target ? Math.min(length, 350 * dt) : 300 * dt;
      if (length) { p.x += dx / length * step; p.y += dy / length * step; }
      p.x = P.clamp(p.x, 22, P.W - 22); p.y = P.clamp(p.y, 30, P.H - 26);
      p.fire -= dt;
      if (p.fire <= 0) {
        p.fire = 0.15;
        for (let i = 0; i < p.weapon; i++) this.shots.push(P.bullet(p.x + (i - (p.weapon - 1) / 2) * 15, p.y - 26, (i - (p.weapon - 1) / 2) * 28, -590));
        this.emit('shot', p);
      }
      if (!this.boss && this.time >= this.nextBoss) {
        this.boss = P.enemy('boss', this.level, this.rng); this.enemies.push(this.boss);
      }
      this.spawn -= dt;
      if (!this.boss && this.spawn <= 0) {
        this.spawn = Math.max(0.28, 1 - this.level * 0.07);
        this.enemies.push(P.enemy(['scout', 'weaver', 'tank'][Math.floor(this.rng() * 3)], this.level, this.rng));
      }
      for (const e of this.enemies) {
        if (e.dead) continue;
        e.age += dt; e.flash = Math.max(0, e.flash - dt);
        if (e.type === 'boss' && e.y >= 105) { e.x = 240 + Math.sin(e.age * 0.7) * 135; }
        else e.y += e.speed * dt;
        if (e.type === 'weaver') e.x = P.clamp(e.x + Math.sin(e.age * 3 + e.phase) * 100 * dt, 25, 455);
        e.fire -= dt;
        if (e.type !== 'scout' && e.y > 0 && e.fire <= 0) {
          e.fire = e.type === 'boss' ? 0.65 : 1.7;
          const angle = Math.atan2(p.y - e.y, p.x - e.x), count = e.type === 'boss' ? 5 : 1;
          for (let i = 0; i < count; i++) { const a = angle + (i - (count - 1) / 2) * 0.23; this.bullets.push(P.bullet(e.x, e.y + e.r, Math.cos(a) * 175, Math.sin(a) * 175, true)); }
        }
        if (P.hit(p, e)) { this.damage(); if (e.type !== 'boss') e.dead = true; }
        if (e.y > P.H + 80) e.dead = true;
      }
      for (const b of [...this.shots, ...this.bullets]) {
        b.x += b.vx * dt; b.y += b.vy * dt;
        if (b.y < -30 || b.y > P.H + 30 || b.x < -30 || b.x > P.W + 30) b.dead = true;
        if (b.hostile) { if (P.hit(b, p)) { b.dead = true; this.damage(); } }
        else for (const e of this.enemies) if (P.hit(b, e)) { b.dead = true; e.hp--; e.flash = 0.07; if (e.hp <= 0) this.kill(e); break; }
      }
      for (const item of this.items) { item.age += dt; item.y += 85 * dt; if (P.hit(item, p)) this.collect(item); if (item.y > P.H + 20) item.dead = true; }
      for (const key of ['enemies', 'shots', 'bullets', 'items']) this[key] = this[key].filter(e => !e.dead);
    }
  }
  P.Game = Game;
  if (typeof module !== 'undefined' && module.exports) module.exports = Game;
})(globalThis);
