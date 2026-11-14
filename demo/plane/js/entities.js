/* ===== 实体与几何：纯数据层，不引用 DOM，浏览器与 Node 共用。 =====
 * 【2026-11-10】碰撞采用圆形判定：飞机装饰翼尖不参与，给躲弹留余地。
 * 所有速度单位为逻辑像素/秒，计时单位为秒；与屏幕 DPR 无关。
 * 【2026-11-11】擦弹：敌弹带 grazed 标记，同一发只擦一次；擦弹是
 * "贴身而过"而非命中，距离在 [hitR, hitR+GRAZE] 内触发。
 */
(function (root) {
  'use strict';
  const P = root.Plane = root.Plane || {};
  // 480×720 是固定逻辑战场；CSS 缩放不改变难度与碰撞范围。
  P.W = 480; P.H = 720;
  // 擦弹阈值：敌弹与玩家距离在 [hitR, hitR+40] 内触发，同一发只擦一次。
  P.GRAZE = 40;
  /** 限制数值到闭区间；调用方保证输入是有限数。 */
  P.clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  /** 输入两个带 x/y/r 的实体；相切算命中，死亡实体永不重复碰撞。 */
  P.hit = (a, b) => !a.dead && !b.dead && Math.hypot(a.x - b.x, a.y - b.y) <= a.r + b.r;
  /** 创建玩家；不接受外部状态，重开不会继承上一局强化。
   * vx/vy 是键盘移动的速度分量，触摸直接追踪不使用。
   * 【2026-11-14】wingmen：雷霆战机式副机，两个小战机贴身跟随主战机并同步开火。
   * 副机独立计时开火（wingFire），与主炮错开半拍，形成更密集的火力网。
   * wingOffset 是副机相对主战机的偏移量（左/右），由 game 在 update 里更新。 */
  P.player = () => ({ x: 240, y: 610, r: 11, lives: 3, bombs: 2, inv: 2, shield: 0, weapon: 1, weaponType: 1, power: 0, fire: 0, freeze: 0, vx: 0, vy: 0, wingmen: [{ x: 225, y: 620, r: 8, fire: 0.075 }, { x: 255, y: 620, r: 8, fire: 0.075 }] });
  /** type 为 scout/weaver/tank/boss；level 至少 1，rng 返回 [0,1)。
   * 出生于屏幕上方；Boss 固定中点，普通敌机留出两侧安全间距。
   * Boss 带 bossPhase（1/2/3，按血量比例计算）和 phaseFlash（阶段切换闪烁计时）。
   * 【2026-11-13】速度公式 spec[2] + min(60 + level*6, 280)：基础速加等级加成，
   * 上限 280 防止后期敌机快到玩家无法反应，同时比旧的 min(50, level*4) 增长更陡，
   * 让 1-12 级的爬升段有明确的难度递增感。 */
  P.enemy = (type, level, rng) => {
    const spec = { scout: [2, 18, 110, 100], weaver: [4, 21, 83, 180], tank: [9, 29, 49, 350], suicide: [3, 15, 220, 200], boss: [140 + level * 35, 61, 30, 3000] }[type];
    return { type, x: type === 'boss' ? 240 : 40 + rng() * 400, y: -75, r: spec[1], hp: spec[0] + (type === 'boss' ? 0 : Math.floor(level / 3)), maxHp: spec[0] + (type === 'boss' ? 0 : Math.floor(level / 3)), speed: spec[2] + Math.min(60 + level * 6, 280), score: spec[3], age: 0, fire: 1, phase: rng() * Math.PI * 2, flash: 0, dead: false, phaseFlash: 0, bossPhase: type === 'boss' ? 1 : 0, bossAlt: 0, bossSpiral: 0 };
  };
  /** 子弹保持独立 dead 标记；一发子弹最多击中一个目标（除非 pierce>0）。敌弹带 grazed 标记。
   * track: true 时为追踪弹（Boss 第三阶段），每帧微调方向朝玩家。
   * 【2026-11-14】武器系统：shotType 标记子弹类型（spread/laser/missile），
   * pierce 记录激光剩余穿透次数，hits 记录导弹已击中的敌机（防同一目标被多次伤害）。 */
  P.bullet = (x, y, vx, vy, hostile = false, track = false, shotType = 'spread', pierce = 0) => ({ x, y, vx, vy, r: hostile ? 5 : 4, hostile, dead: false, grazed: false, track, shotType, pierce, hits: [] });
  /** 补给种类由核心指定，金色外观由渲染层负责。 */
  P.pickup = (x, y, type) => ({ x, y, type, r: 16, age: 0, dead: false });
  // 条件导出不会在 file:// 的经典 script 中触发 require。
  if (typeof module !== 'undefined' && module.exports) module.exports = P;
})(globalThis);
