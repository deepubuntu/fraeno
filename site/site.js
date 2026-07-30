const header = document.querySelector("[data-header]");
const menuButton = document.querySelector("[data-menu-button]");
const navigation = document.querySelector("[data-navigation]");
const revealItems = document.querySelectorAll("[data-reveal]");
const flowSteps = document.querySelectorAll("[data-flow-step]");
const demoWrap = document.querySelector("[data-demo-wrap]");
const demo = document.querySelector("[data-product-demo]");
const demoButtons = document.querySelectorAll("[data-demo-state]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const demoContent = {
  baseline: {
    title: "The robot behaved like before.",
    detail: "Messages kept moving through the complete system.",
    sensor: "20.6 Hz",
    controller: "20.5 Hz",
    command: "20.4 Hz",
    decision: "Ready for review",
  },
  updated: {
    title: "Messages stopped at the controller.",
    detail: "The update built. The robot did not behave like before.",
    sensor: "20.6 Hz",
    controller: "No messages",
    command: "0 Hz",
    decision: "Blocked before deployment",
  },
};

const updateHeader = () => {
  header?.classList.toggle("is-scrolled", window.scrollY > 18);
};

const closeMenu = () => {
  navigation?.classList.remove("is-open");
  menuButton?.setAttribute("aria-expanded", "false");
};

const setDemoState = (state) => {
  const content = demoContent[state];

  if (!demo || !content) {
    return;
  }

  demo.dataset.state = state;
  demo.querySelector("[data-demo-title]").textContent = content.title;
  demo.querySelector("[data-demo-detail]").textContent = content.detail;
  demo.querySelector("[data-sensor-rate]").textContent = content.sensor;
  demo.querySelector("[data-controller-rate]").textContent = content.controller;
  demo.querySelector("[data-command-rate]").textContent = content.command;
  demo.querySelector("[data-demo-decision]").textContent = content.decision;

  demoButtons.forEach((button) => {
    const selected = button.dataset.demoState === state;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
};

const updateDemoLift = () => {
  if (!demo || !demoWrap || reducedMotion.matches) {
    return;
  }

  const rect = demoWrap.getBoundingClientRect();
  const viewportMiddle = window.innerHeight / 2;
  const demoMiddle = rect.top + rect.height / 2;
  const distance = (demoMiddle - viewportMiddle) / window.innerHeight;
  const lift = Math.max(-14, Math.min(14, distance * -12));
  demo.style.setProperty("--scroll-lift", `${lift.toFixed(2)}px`);
};

let frame = 0;
const onScroll = () => {
  updateHeader();

  if (!frame) {
    frame = window.requestAnimationFrame(() => {
      updateDemoLift();
      frame = 0;
    });
  }
};

menuButton?.addEventListener("click", () => {
  const open = navigation?.classList.toggle("is-open") ?? false;
  menuButton.setAttribute("aria-expanded", String(open));
});

navigation?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", closeMenu);
});

document.addEventListener("click", (event) => {
  if (!header?.contains(event.target)) {
    closeMenu();
  }
});

demoButtons.forEach((button) => {
  button.addEventListener("click", () => setDemoState(button.dataset.demoState));
});

demo?.addEventListener("pointermove", (event) => {
  if (reducedMotion.matches || event.pointerType === "touch") {
    return;
  }

  const bounds = demo.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  demo.style.setProperty("--pointer-x", `${(x * 1.8).toFixed(2)}deg`);
  demo.style.setProperty("--pointer-y", `${(y * -1.4).toFixed(2)}deg`);
});

demo?.addEventListener("pointerleave", () => {
  demo.style.setProperty("--pointer-x", "0deg");
  demo.style.setProperty("--pointer-y", "0deg");
});

if (reducedMotion.matches || !("IntersectionObserver" in window)) {
  revealItems.forEach((item) => item.classList.add("is-visible"));
  flowSteps.forEach((item) => {
    item.classList.add("is-visible");
    item.classList.add("is-active");
  });
} else {
  const revealObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -12% 0px", threshold: 0.12 },
  );

  revealItems.forEach((item) => revealObserver.observe(item));

  const flowObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        const index = Number(entry.target.querySelector("span")?.textContent) || 1;
        window.setTimeout(() => {
          entry.target.classList.add("is-visible");
          entry.target.classList.add("is-active");
        }, (index - 1) * 100);
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -10% 0px", threshold: 0.25 },
  );

  flowSteps.forEach((item) => flowObserver.observe(item));
}

reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches && demo) {
    demo.style.setProperty("--pointer-x", "0deg");
    demo.style.setProperty("--pointer-y", "0deg");
    demo.style.setProperty("--scroll-lift", "0px");
  }
});

window.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", updateDemoLift);
setDemoState("updated");
onScroll();
