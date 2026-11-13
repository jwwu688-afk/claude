/* 长跑冒烟：模拟 5 分钟游戏，覆盖 Boss、时停、连击等全部机制，确认不崩溃。 */
'use strict';
const P = require('../js/entities.js');
const Game = require('../js/game.js');
const g = new Game(() => 0.5, () => {});
g.state = 'playing';
// 给玩家持续无敌，模拟不死长跑，确保覆盖 Boss 出场、阶段切换、时停等全部机制。
g.p.inv = 99999;
// 不移动，玩家停在中心 x=240，敌机（rng=0.5）也从 x=240 出生，子弹能命中，
// 否则玩家跑到边缘而敌机在中心，击杀永远为 0，长跑只覆盖了擦弹。
for (let i = 0; i < Math.ceil(300 / 0.016); i++) {
  g.update(0.016, { x: 0, y: 0 });
}
console.log('状态:', g.state);
console.log('得分:', g.score);
console.log('击杀:', g.kills);
console.log('最高连击:', g.bestCombo);
console.log('波次:', g.level);
console.log('存活:', g.time.toFixed(1) + 's');
console.log('敌机/敌弹/补给:', g.enemies.length, g.bullets.length, g.items.length);
console.log('长跑冒烟通过');
