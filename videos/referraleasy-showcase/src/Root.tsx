import { Composition, Folder } from "remotion";
import { Showcase } from "./Showcase";
import { Opening } from "./scenes/Opening";
import { Journey } from "./scenes/Journey";
import { Architecture } from "./scenes/Architecture";
import { Booking } from "./scenes/Booking";
import { Continuity } from "./scenes/Continuity";
import { Recovery } from "./scenes/Recovery";
import { Closing, Tagline } from "./scenes/Closing";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="ReferralEasy"
        component={Showcase}
        durationInFrames={1800}
        fps={30}
        width={1920}
        height={1080}
      />
      <Folder name="Scenes">
        <Composition
          id="Opening"
          component={Opening}
          durationInFrames={180}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="PatientJourney"
          component={Journey}
          durationInFrames={360}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="Architecture"
          component={Architecture}
          durationInFrames={270}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="BookingIntegrity"
          component={Booking}
          durationInFrames={270}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="DurableContinuity"
          component={Continuity}
          durationInFrames={210}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="SafeRecovery"
          component={Recovery}
          durationInFrames={300}
          fps={30}
          width={1920}
          height={1080}
        />
        <Composition
          id="Closing"
          component={Closing}
          durationInFrames={210}
          fps={30}
          width={1920}
          height={1080}
        />
      </Folder>
      <Folder name="Elements">
        <Composition
          id="Tagline"
          component={Tagline}
          durationInFrames={90}
          fps={30}
          width={1920}
          height={1080}
          defaultProps={{ children: "A simpler journey. Control built in." }}
        />
      </Folder>
    </>
  );
};
