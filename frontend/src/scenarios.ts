import type { Participant, Scenario } from './api'

export type Step =
  | {kind: 'message'; sender: Participant; content: string; delay: number; ref?: 'alice' | 'bob' | 'jack' | 'revision'; expect?: 'independent' | 'dependent' | 'revision'}
  | {kind: 'await-decision'; delay: number}
  | {kind: 'await-answer'; ref: 'jack'; delay: number}
  | {kind: 'await-pitch'; delay: number}
  | {kind: 'cancel'; delay: number}
  | {kind: 'await-clarification'; delay: number}

export const scenarios: Record<Scenario, {title: string; caption: string; steps: Step[]}> = {
  main: {
    title: 'Revise while working', caption: 'The pitch waits for Alice’s update', steps: [
      {kind: 'message', sender: 'alice', content: "Morning guys! We should probably finalize what we're showing tomorrow.", delay: 350},
      {kind: 'message', sender: 'bob', content: 'Agreed. I think we have the idea, but not really the pitch yet.', delay: 1000},
      {kind: 'message', sender: 'jack', content: "I can put the pitch together. We just need to decide what features we're actually selling.", delay: 1500},
      {kind: 'message', sender: 'alice', content: '@CastleBot suggest three core features for StudyBuddy, a study-planning app for university students. Keep it brief.', ref: 'alice', expect: 'independent', delay: 1100},
      {kind: 'message', sender: 'bob', content: '@CastleBot give us three low-budget ideas for launching StudyBuddy on campus.', ref: 'bob', expect: 'independent', delay: 350},
      {kind: 'message', sender: 'jack', content: "@CastleBot use Alice's feature suggestions and Bob's launch ideas to write a 45-second pitch for our presentation.", ref: 'jack', expect: 'dependent', delay: 500},
      {kind: 'message', sender: 'alice', content: 'Wait, I forgot something. Most students will probably use this during exam season.', delay: 950},
      {kind: 'message', sender: 'bob', content: "Yeah, that's a better angle actually.", delay: 900},
      {kind: 'message', sender: 'alice', content: '@CastleBot update my earlier feature suggestions to focus on exam preparation and revision planning rather than everyday scheduling.', ref: 'revision', expect: 'revision', delay: 850},
      {kind: 'await-decision', delay: 250},
      {kind: 'await-pitch', delay: 200},
      {kind: 'message', sender: 'jack', content: 'Okay, that sounds much better.', delay: 500},
      {kind: 'message', sender: 'bob', content: 'I think we can actually present this tomorrow.', delay: 850},
      {kind: 'message', sender: 'alice', content: "Perfect. I'll handle the slides.", delay: 900},
    ]
  },
  revision_after: {
    title: 'Revise after the pitch', caption: 'A completed answer stays in the chat', steps: [
      {kind: 'message', sender: 'alice', content: 'Let’s get the first StudyBuddy pitch ready.', delay: 350},
      {kind: 'message', sender: 'alice', content: '@CastleBot suggest three core features for StudyBuddy, a study-planning app for university students. Keep it brief.', ref: 'alice', expect: 'independent', delay: 650},
      {kind: 'message', sender: 'bob', content: '@CastleBot give us three low-budget ideas for launching StudyBuddy on campus.', ref: 'bob', expect: 'independent', delay: 400},
      {kind: 'message', sender: 'jack', content: "@CastleBot use Alice's feature suggestions and Bob's launch ideas to write a 45-second pitch for our presentation.", ref: 'jack', expect: 'dependent', delay: 450},
      {kind: 'await-answer', ref: 'jack', delay: 100},
      {kind: 'message', sender: 'jack', content: 'Nice, the first pitch is ready.', delay: 400},
      {kind: 'message', sender: 'alice', content: 'I just realized we should focus on exam season instead.', delay: 700},
      {kind: 'message', sender: 'alice', content: '@CastleBot update my earlier feature suggestions to focus on exam preparation and revision planning rather than everyday scheduling.', ref: 'revision', expect: 'revision', delay: 600},
      {kind: 'await-decision', delay: 150},
      {kind: 'await-pitch', delay: 150},
      {kind: 'message', sender: 'jack', content: 'Now we have both versions to compare.', delay: 550},
    ]
  },
  cancellation: {
    title: 'Changing course', caption: 'A request cancelled in real time', steps: [
      {kind: 'message', sender: 'bob', content: '@CastleBot create a detailed 10-step checklist for launching StudyBuddy at universities.', ref: 'bob', expect: 'independent', delay: 400},
      {kind: 'message', sender: 'bob', content: "Actually, never mind. Let's skip that checklist.", delay: 700},
      {kind: 'cancel', delay: 250},
    ]
  },
  ambiguity: {
    title: 'Which one?', caption: 'When context needs clarification', steps: [
      {kind: 'message', sender: 'alice', content: '@CastleBot suggest a tagline for our StudyBuddy app.', ref: 'alice', expect: 'independent', delay: 400},
      {kind: 'message', sender: 'alice', content: '@CastleBot outline a short pitch for our StudyBuddy app.', ref: 'bob', expect: 'independent', delay: 650},
      {kind: 'message', sender: 'alice', content: '@CastleBot can you update that earlier thing?', delay: 700},
      {kind: 'await-clarification', delay: 250},
    ]
  }
}
