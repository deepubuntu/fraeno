const header = document.querySelector("[data-header]");
const menuButton = document.querySelector("[data-menu-button]");
const navigation = document.querySelector("[data-navigation]");
const hero = document.querySelector("[data-hero]");
const heroVideo = document.querySelector("[data-hero-video]");
const proof = document.querySelector("[data-proof]");
const sectionVideo = document.querySelector("[data-section-video]");
const actionVideo = document.querySelector(".action-video");
const proofMoments = Array.from(document.querySelectorAll("[data-proof-moment]"));
const revealItems = document.querySelectorAll("[data-reveal]");
const methodSteps = document.querySelectorAll("[data-step]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const updateHeader = () => {
  header?.classList.toggle("is-scrolled", window.scrollY > 20);
};

const closeMenu = () => {
  navigation?.classList.remove("is-open");
  menuButton?.classList.remove("is-open");
  menuButton?.setAttribute("aria-expanded", "false");
};

const updateHero = () => {
  if (!hero || reducedMotion.matches) {
    return;
  }

  const bounds = hero.getBoundingClientRect();
  const progress = Math.min(Math.max(-bounds.top / Math.max(bounds.height, 1), 0), 1);
  hero.style.setProperty("--hero-progress", progress.toFixed(3));
};

const updateProof = () => {
  if (!proof || !proofMoments.length || reducedMotion.matches) {
    return;
  }

  const bounds = proof.getBoundingClientRect();
  const travel = Math.max(bounds.height - window.innerHeight, 1);
  const progress = Math.min(Math.max(-bounds.top / travel, 0), 1);
  const momentProgress = Math.min(progress / 0.72, 0.999);
  const activeIndex = Math.min(
    Math.floor(momentProgress * proofMoments.length),
    proofMoments.length - 1,
  );
  const traceProgress = Math.min(Math.max((progress - 0.72) / 0.16, 0), 1);

  proof.style.setProperty("--proof-progress", progress.toFixed(3));
  proof.style.setProperty("--proof-brightness", (0.42 + progress * 0.07).toFixed(3));
  proof.style.setProperty("--proof-shift-x", `${(-progress * 1.5).toFixed(3)}%`);
  proof.style.setProperty("--proof-shift-y", `${(-progress).toFixed(3)}%`);
  proof.style.setProperty("--proof-scale", (1.08 + progress * 0.025).toFixed(3));
  proof.style.setProperty("--trace-progress", traceProgress.toFixed(3));
  proof.style.setProperty("--trace-offset", `${((1 - traceProgress) * 1.25).toFixed(3)}rem`);
  proof.style.setProperty(
    "--proof-next-offset",
    `${(proofMoments[activeIndex].getBoundingClientRect().height + 16).toFixed(1)}px`,
  );
  proof.dataset.activeMoment = String(activeIndex + 1);

  proofMoments.forEach((moment, index) => {
    moment.classList.toggle("is-active", index === activeIndex);
    moment.classList.toggle("is-next", index === activeIndex + 1);
    moment.classList.toggle("is-past", index < activeIndex);
  });
};

let scrollFrame = 0;
const onScroll = () => {
  updateHeader();

  if (!scrollFrame) {
    scrollFrame = window.requestAnimationFrame(() => {
      updateHero();
      updateProof();
      scrollFrame = 0;
    });
  }
};

menuButton?.addEventListener("click", () => {
  const isOpen = navigation?.classList.toggle("is-open") ?? false;
  menuButton.classList.toggle("is-open", isOpen);
  menuButton.setAttribute("aria-expanded", String(isOpen));
});

navigation?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", closeMenu);
});

document.addEventListener("click", (event) => {
  if (!header?.contains(event.target)) {
    closeMenu();
  }
});

hero?.addEventListener("pointermove", (event) => {
  if (reducedMotion.matches || event.pointerType === "touch") {
    return;
  }

  const bounds = hero.getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width - 0.5;
  const y = (event.clientY - bounds.top) / bounds.height - 0.5;
  hero.style.setProperty("--pointer-x", `${(x * 12).toFixed(2)}px`);
  hero.style.setProperty("--pointer-y", `${(y * 8).toFixed(2)}px`);
});

hero?.addEventListener("pointerleave", () => {
  hero.style.setProperty("--pointer-x", "0px");
  hero.style.setProperty("--pointer-y", "0px");
});

if (reducedMotion.matches || !("IntersectionObserver" in window)) {
  revealItems.forEach((item) => item.classList.add("is-visible"));
  methodSteps.forEach((item) => item.classList.add("is-visible"));
  proof?.classList.add("is-visible");
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

  const stepObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) {
          return;
        }

        const index = Array.from(methodSteps).indexOf(entry.target);
        window.setTimeout(() => entry.target.classList.add("is-visible"), index * 90);
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.2 },
  );

  methodSteps.forEach((item) => stepObserver.observe(item));

  if (proof) {
    const proofObserver = new IntersectionObserver(
      (entries) => {
        proof.classList.toggle("is-visible", entries.some((entry) => entry.isIntersecting));
      },
      { threshold: 0.2 },
    );
    proofObserver.observe(proof);
  }
}

let videoObserver;

const configureHeroVideo = () => {
  if (!heroVideo) {
    return;
  }

  heroVideo.controls = false;

  if (reducedMotion.matches) {
    heroVideo.pause();
    heroVideo.currentTime = 0;
    return;
  }

  heroVideo.play().catch(() => {});
};

heroVideo?.addEventListener("canplay", configureHeroVideo);
window.addEventListener("pageshow", configureHeroVideo);

const configureSectionVideo = () => {
  videoObserver?.disconnect();

  if (!sectionVideo) {
    return;
  }

  if (reducedMotion.matches) {
    sectionVideo.pause();
    sectionVideo.currentTime = 0;
    return;
  }

  if (!("IntersectionObserver" in window)) {
    sectionVideo.play().catch(() => {});
    return;
  }

  videoObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          sectionVideo.play().catch(() => {});
        } else {
          sectionVideo.pause();
        }
      });
    },
    { rootMargin: "80px 0px", threshold: 0.2 },
  );
  videoObserver.observe(sectionVideo);
};

let actionVideoObserver;

const configureActionVideo = () => {
  actionVideoObserver?.disconnect();

  if (!actionVideo) {
    return;
  }

  actionVideo.controls = false;

  if (reducedMotion.matches) {
    actionVideo.pause();
    actionVideo.currentTime = 0;
    return;
  }

  if (!("IntersectionObserver" in window)) {
    actionVideo.play().catch(() => {});
    return;
  }

  actionVideoObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          actionVideo.play().catch(() => {});
        } else {
          actionVideo.pause();
        }
      });
    },
    { rootMargin: "80px 0px", threshold: 0.2 },
  );
  actionVideoObserver.observe(actionVideo);
};

const nudgeActionVideo = () => {
  if (reducedMotion.matches || !actionVideo) {
    return;
  }
  if (actionVideo.paused) {
    actionVideo.play().catch(() => {});
  }
};

actionVideo?.addEventListener("canplay", nudgeActionVideo);
window.addEventListener("touchstart", nudgeActionVideo, { once: true, passive: true });
window.addEventListener("pageshow", configureActionVideo);

reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches && hero) {
    hero.style.setProperty("--pointer-x", "0px");
    hero.style.setProperty("--pointer-y", "0px");
    hero.style.setProperty("--hero-progress", "0");
  }
  if (reducedMotion.matches && proof) {
    proof.style.setProperty("--proof-progress", "0");
    proof.style.setProperty("--proof-brightness", "0.42");
    proof.style.setProperty("--proof-shift-x", "0%");
    proof.style.setProperty("--proof-shift-y", "0%");
    proof.style.setProperty("--proof-scale", "1.08");
    proof.style.setProperty("--proof-next-offset", "2.15em");
    proof.style.setProperty("--trace-progress", "1");
    proof.style.setProperty("--trace-offset", "0rem");
  }
  configureHeroVideo();
  configureSectionVideo();
  configureActionVideo();
});

window.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", () => {
  updateHero();
  updateProof();
});
configureHeroVideo();
configureSectionVideo();
configureActionVideo();
onScroll();

const contactOverlay = document.querySelector("[data-contact-overlay]");
const contactForm = document.querySelector("[data-contact-form]");
const contactStatus = document.querySelector("[data-contact-status]");
let contactOpenedAt = 0;

function openContact() {
  contactOverlay.hidden = false;
  document.body.style.overflow = "hidden";
  contactOpenedAt = Date.now();
  const first = contactForm.querySelector('input[name="name"]');
  if (first) {
    first.focus();
  }
}

function closeContact() {
  contactOverlay.hidden = true;
  document.body.style.overflow = "";
}

for (const trigger of document.querySelectorAll("[data-contact-open]")) {
  trigger.addEventListener("click", openContact);
}

if (contactOverlay) {
  contactOverlay.addEventListener("click", (event) => {
    if (event.target === contactOverlay) {
      closeContact();
    }
  });
  const closeButton = contactOverlay.querySelector("[data-contact-close]");
  if (closeButton) {
    closeButton.addEventListener("click", closeContact);
  }
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !contactOverlay.hidden) {
      closeContact();
    }
  });
}

if (contactForm) {
  contactForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = contactForm.querySelector(".contact-submit");
    const booking = contactOverlay.querySelector("[data-contact-book]");
    const fields = new FormData(contactForm);
    contactStatus.textContent = "";
    contactStatus.className = "contact-status";
    booking.hidden = true;
    submit.disabled = true;
    try {
      const response = await fetch("/api/contact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: fields.get("name"),
          email: fields.get("email"),
          company: fields.get("company"),
          message: fields.get("message"),
          website: fields.get("website"),
          updates: fields.get("updates") === "on",
          dwell_ms: Date.now() - contactOpenedAt,
        }),
      });
      const result = await response.json();
      if (result.ok) {
        contactForm.reset();
        contactStatus.textContent =
          "Request sent. We reply within one business day.";
        contactStatus.className = "contact-status is-success";
        booking.hidden = false;
      } else {
        contactStatus.textContent =
          "That did not go through: " +
          result.reason +
          ". You can also email thabhelo@deepubuntu.com.";
        contactStatus.className = "contact-status is-error";
      }
    } catch {
      contactStatus.textContent =
        "The request failed to send. You can also email thabhelo@deepubuntu.com.";
      contactStatus.className = "contact-status is-error";
    } finally {
      submit.disabled = false;
    }
  });
}
