const header = document.querySelector("[data-header]");
const menuButton = document.querySelector("[data-menu-button]");
const navigation = document.querySelector("[data-navigation]");
const revealItems = document.querySelectorAll("[data-reveal]");
const hero = document.querySelector("[data-cinematic-hero]");
const heroVideo = document.querySelector("[data-hero-video]");
const runVideo = document.querySelector("[data-run-video]");
const runStage = document.querySelector("[data-run-stage]");
const runStatus = document.querySelector("[data-run-status]");
const runMeter = document.querySelector("[data-run-meter]");
const runSteps = document.querySelectorAll("[data-run-step]");
const runLines = document.querySelectorAll("[data-run-line]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const videos = [heroVideo, runVideo].filter(Boolean);

const runStates = [
  "Update found",
  "Trusted system running",
  "Behavior comparison running",
  "Regression blocked",
];

const updateHeader = () => {
  header?.classList.toggle("is-scrolled", window.scrollY > 24);
};

const closeMenu = () => {
  navigation?.classList.remove("is-open");
  menuButton?.setAttribute("aria-expanded", "false");
};

const activateRunStep = (phase) => {
  const phaseNumber = Number(phase);
  runStage?.setAttribute("data-phase", String(phaseNumber));

  if (runStatus) {
    runStatus.textContent = runStates[phaseNumber - 1];
  }

  if (runMeter) {
    runMeter.style.width = `${phaseNumber * 25}%`;
  }

  runSteps.forEach((step) => {
    step.classList.toggle("is-active", step.getAttribute("data-run-step") === String(phaseNumber));
  });

  runLines.forEach((line) => {
    const lineNumber = Number(line.getAttribute("data-run-line"));
    const lineState = line.querySelector("i");
    line.classList.toggle("is-complete", lineNumber < phaseNumber);
    line.classList.toggle("is-active", lineNumber === phaseNumber);

    if (lineState) {
      lineState.textContent =
        lineNumber < phaseNumber ? "done" : lineNumber === phaseNumber ? "running" : "waiting";
    }
  });
};

const updateHeroProgress = () => {
  if (!hero || reducedMotion) {
    return;
  }

  const rect = hero.getBoundingClientRect();
  const range = Math.max(rect.height - window.innerHeight, 1);
  const progress = Math.min(Math.max(-rect.top / range, 0), 1);
  hero.style.setProperty("--hero-progress", progress.toFixed(3));
};

let scrollFrame = 0;
const handleScroll = () => {
  updateHeader();

  if (!scrollFrame) {
    scrollFrame = window.requestAnimationFrame(() => {
      updateHeroProgress();
      scrollFrame = 0;
    });
  }
};

menuButton?.addEventListener("click", () => {
  const isOpen = navigation?.classList.toggle("is-open") ?? false;
  menuButton.setAttribute("aria-expanded", String(isOpen));
});

navigation?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", closeMenu);
});

if (reducedMotion) {
  videos.forEach((video) => {
    video.pause();
    video.addEventListener(
      "loadedmetadata",
      () => {
        video.currentTime = Math.min(3, video.duration || 3);
      },
      { once: true },
    );
  });
} else {
  videos.forEach((video) => {
    video.play().catch(() => {
      video.controls = true;
    });
  });
}

document.addEventListener("visibilitychange", () => {
  if (reducedMotion) {
    return;
  }

  videos.forEach((video) => {
    if (document.hidden) {
      video.pause();
    } else {
      video.play().catch(() => undefined);
    }
  });
});

if (reducedMotion || !("IntersectionObserver" in window)) {
  revealItems.forEach((item) => item.classList.add("is-visible"));
  activateRunStep(1);
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
    { rootMargin: "0px 0px -10% 0px", threshold: 0.12 },
  );

  const runObserver = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((left, right) => right.intersectionRatio - left.intersectionRatio);

      if (visible[0]) {
        activateRunStep(visible[0].target.getAttribute("data-run-step"));
      }
    },
    { rootMargin: "-34% 0px -42% 0px", threshold: [0, 0.25, 0.5, 0.75] },
  );

  revealItems.forEach((item) => revealObserver.observe(item));
  runSteps.forEach((step) => runObserver.observe(step));
  activateRunStep(1);
}

window.addEventListener("scroll", handleScroll, { passive: true });
window.addEventListener("resize", updateHeroProgress);
handleScroll();
