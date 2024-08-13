(() => {
  let button = document.getElementsByClassName('user-tap-button')[0]

  let click = () => {
    button.dispatchEvent(new PointerEvent('pointerdown'))
    button.dispatchEvent(new PointerEvent('pointerup'))
  };

  for (let i = 0; i < 7500 / 11; i++) {
    click();
  }
})();
