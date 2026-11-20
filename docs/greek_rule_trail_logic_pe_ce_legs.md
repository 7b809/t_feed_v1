# Greek-Rule Trail Logic (Rule Stated in Advance, Applied Only to Completed Candles)

---

## Part 1: Put (PE) Leg Rules & Completed-Candle Example (Reference)

A non-future-biased Greek trail rule for a **PE leg** (where more negative $\Delta$ indicates deeper In-The-Money / more favorable movement):

* **Rule A (Delta trail):** Once $\Delta \le -0.60$ (more negative $=$ more ITM for PE), raise the stop to $\text{entry} + 20$ and trail by $15$ points below the last completed close.
* **Rule B (Gamma/Theta profit-jump):** If $\Delta \le -0.65$ and $\Theta$ is less negative than $-38$ (i.e., $\Theta > -38$, meaning slower decay per day), jump the target by $+15$ points (i.e., let the winner run) and move the stop to $\text{last close} - 10$.
* **Rule C (Vega warning):** If Vega drops $> 0.25$ from entry while $\Delta$ is still improving, do not tighten — Vega crush can mask Delta gains; keep the original trail.

### PE Leg — Completed-Candle Example

| Time | Completed candle | $\Delta$ | $\Theta$ | Rule fired | Action (no future data) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 10:08 | 10:08 close 126.85 | $-0.5893$ | $-38.65$ | A nearly ($\Delta$ $-0.589$) | Watch |
| 10:09 | 10:09 close 126.65 | $-0.5921$ | $-37.89$ | A + B (per provided log) | Stop $\rightarrow$ entry+20; target jump $+15$ |
| 10:24 | 10:24 close 139.40 | $-0.6165$ | $-38.44$ | A | Stop $\rightarrow$ entry+20 (already), trail to $\text{close} - 15 = 124.40$ |
| 10:30 | 10:30 close 151.35 | $-0.6487$ | $-37.17$ | A + B | Target jump again; trail to $136.35$ |
| 10:33 | 10:33 close 168.75 | $-0.6849$ | $-35.77$ | A + B | Trail to $153.75$; target extended |

---

## Part 2: Opposite Greek-Rule Trail Logic for Call (CE) Leg

For a **CE leg** (assuming a long CE position where $\Delta > 0$ increases toward $+1.00$ as the option moves deeper ITM, and $\Theta < 0$ represents time decay):

* **Rule A — Opposite (Delta trail for CE):** Once $\Delta \ge +0.60$ (more positive $=$ more ITM for CE), raise the stop to $\text{entry} + 20$ and trail by $15$ points below the last completed close.
* **Rule B — Opposite (Gamma/Theta profit-jump for CE):** If $\Delta \ge +0.65$ and $\Theta$ is less negative than $-38$ (i.e., $\Theta > -38$, slower decay), jump the target by $+15$ points (let the winner run) and move the stop to $\text{last close} - 10$.
* **Rule C — Opposite (Vega warning for CE):** If Vega drops $> 0.25$ from entry while $\Delta$ is still improving (rising toward $+1.00$), do not tighten — Vega crush can mask Delta gains; keep the original trail.

*(Note: Because option prices and stop/target point offsets in a long CE trade move upward on favorable price action just like a long PE trade's premium, the point offsets—such as $\text{entry} + 20$, $\text{close} - 15$, target $+15$, and $\text{close} - 10$—stay in positive premium terms, while the **Delta threshold sign is inverted** from $\Delta \le -0.60 / -0.65$ to $\Delta \ge +0.60 / +0.65$.)*

### CE Leg — Completed-Candle Example

| Time | Completed candle | $\Delta$ | $\Theta$ | Rule fired | Action (no future data) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 10:08 | 10:08 close 126.85 | $+0.5893$ | $-38.65$ | A nearly ($\Delta$ $+0.589$) | Watch |
| 10:09 | 10:09 close 126.65 | $+0.5921$ | $-37.89$ | Watch / $\Theta$ improving ($\Delta < +0.60$) | Keep initial stop; note $\Theta > -38$ for upcoming $\Delta \ge +0.65$ |
| 10:24 | 10:24 close 139.40 | $+0.6165$ | $-38.44$ | A | Raise stop to $\text{entry} + 20$; trail to $\text{close} - 15 = 124.40$ |
| 10:30 | 10:30 close 151.35 | $+0.6487$ | $-37.17$ | A (approaching B at $\Delta \ge +0.65$) | Trail to $\text{close} - 15 = 136.35$; watch for $\Delta \ge +0.65$ with $\Theta > -38$ |
| 10:33 | 10:33 close 168.75 | $+0.6849$ | $-35.77$ | A + B | Target jump $+15$ (let winner run); move stop to $\text{close} - 10 = 158.75$ (or trail $153.75$ if Rule A trail is retained) |